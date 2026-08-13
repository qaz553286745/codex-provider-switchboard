from __future__ import annotations

import json
import logging
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from .. import __version__
from ..domain.bridge import request_summary
from ..infrastructure.codex_config import CodexConfigManager
from ..infrastructure.config_store import PROVIDER_IDS, ConfigStore
from ..infrastructure.credential_store import CredentialStore, CredentialStoreError
from ..infrastructure.cursor_client import CursorBackendError
from ..infrastructure.custom_client import CustomAPIError, CustomResponsesClient
from ..infrastructure.direct_catalog import direct_platform, direct_platform_catalog
from ..infrastructure.direct_client import DirectAPIError, DirectClient
from ..infrastructure.kiro_cli import KiroInvocationError, KiroRunner
from ..infrastructure.oauth import OAuthError, OAuthLoginManager
from ..providers.base import ProviderError, ProviderResponse, ResponsesProvider
from ..providers.cursor import CursorProvider
from ..providers.direct import DirectProvider
from ..settings import AppSettings, default_data_dir
from .inspector import RequestInspector

logger = logging.getLogger(__name__)


class SwitchboardService:
    def __init__(
        self,
        settings: AppSettings,
        store: ConfigStore,
        kiro_runner: KiroRunner,
        cursor_provider: CursorProvider,
        custom_client: CustomResponsesClient,
        direct_client: DirectClient,
        direct_provider: DirectProvider,
        credentials: CredentialStore,
        oauth: OAuthLoginManager,
        codex_config: CodexConfigManager,
        inspector: RequestInspector,
        providers: dict[str, ResponsesProvider],
    ) -> None:
        missing = PROVIDER_IDS - providers.keys()
        if missing:
            raise ValueError(f"Missing providers: {', '.join(sorted(missing))}")
        self.settings = settings
        self.store = store
        self.kiro_runner = kiro_runner
        self.cursor_provider = cursor_provider
        self.custom_client = custom_client
        self.direct_client = direct_client
        self.direct_provider = direct_provider
        self.credentials = credentials
        self.oauth = oauth
        self.codex_config = codex_config
        self.inspector = inspector
        self.providers = providers

    def active_provider_id(self) -> str:
        provider = self.store.read().get("active_provider")
        return provider if provider in PROVIDER_IDS else "kiro"

    def active_provider(self) -> ResponsesProvider:
        return self.providers[self.active_provider_id()]

    @staticmethod
    def _reject_native_remote_compaction(body: dict[str, Any]) -> None:
        input_value = body.get("input")
        if not isinstance(input_value, list) or not any(
            isinstance(item, dict) and item.get("type") == "compaction_trigger"
            for item in input_value
        ):
            return
        logger.warning("Rejected unsupported native remote compaction request")
        raise ProviderError(
            "Native OpenAI remote compaction is not supported by Switchboard. "
            "Enable the dedicated codex-provider-switchboard provider and start "
            "a new Codex task.",
            error_type="unsupported_feature",
            status_code=400,
        )

    def _log_request(self, body: dict[str, Any], provider_id: str) -> None:
        if not self.settings.debug_requests:
            return
        summary = request_summary(body)
        summary["provider"] = provider_id
        logger.info("request_metadata=%s", json.dumps(summary, ensure_ascii=True))

    def _ensure_ready(self, provider_id: str) -> None:
        if provider_id == "cursor":
            if not self.store.api_key():
                raise ProviderError(
                    "Cursor API key is not configured. Open the local control panel "
                    "first.",
                    error_type="cursor_configuration_error",
                    status_code=503,
                )
            if (
                self.cursor_provider.backend_id() == "cli"
                and shutil.which(self.settings.cursor_cli) is None
            ):
                raise ProviderError(
                    f"Cursor Agent CLI was not found: {self.settings.cursor_cli}",
                    error_type="cursor_configuration_error",
                    status_code=503,
                )
        if provider_id == "custom":
            custom = self.store.read()["custom"]
            if not custom.get("base_url") or not self.store.custom_api_key():
                raise ProviderError(
                    "Third-party base URL and API key must be configured first.",
                    error_type="custom_configuration_error",
                    status_code=503,
                )
        if provider_id == "direct":
            platform_id = str(self.store.read()["direct"]["platform_id"])
            try:
                credential, _source = self.credentials.resolve(platform_id)
            except CredentialStoreError as exc:
                raise ProviderError(
                    "Direct-provider credential store could not be read.",
                    error_type="direct_configuration_error",
                    status_code=503,
                ) from exc
            if credential is None:
                raise ProviderError(
                    "Authenticate the selected direct platform first.",
                    error_type="direct_configuration_error",
                    status_code=503,
                )

    async def complete(self, body: dict[str, Any]) -> ProviderResponse:
        provider_id = self.active_provider_id()
        self._reject_native_remote_compaction(body)
        self._ensure_ready(provider_id)
        self._log_request(body, provider_id)
        return await self.providers[provider_id].complete(body)

    def stream(self, body: dict[str, Any]) -> tuple[str, AsyncIterator[bytes]]:
        provider_id = self.active_provider_id()
        self._reject_native_remote_compaction(body)
        self._ensure_ready(provider_id)
        self._log_request(body, provider_id)
        return provider_id, self.providers[provider_id].stream(body)

    @staticmethod
    def safe_model_catalog(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = {
            "id",
            "displayName",
            "description",
            "aliases",
            "parameters",
            "variants",
        }
        return [
            {key: value for key, value in item.items() if key in allowed}
            for item in items
        ]

    async def cursor_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        try:
            items = await self.cursor_provider.get_models(force=force)
        except CursorBackendError as exc:
            raise ProviderError(
                str(exc),
                error_type=f"cursor_{self.cursor_provider.backend_id()}_error",
                status_code=502,
            ) from exc
        return self.safe_model_catalog(items)

    async def kiro_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        try:
            items = await self.kiro_runner.list_models(force=force)
        except KiroInvocationError as exc:
            raise ProviderError(
                str(exc), error_type="kiro_cli_error", status_code=502
            ) from exc
        result: list[dict[str, Any]] = []
        for item in items:
            model_id = item.get("model_id")
            if not isinstance(model_id, str) or not model_id:
                continue
            normalized: dict[str, Any] = {
                "id": model_id,
                "displayName": str(item.get("model_name") or model_id),
            }
            if isinstance(item.get("description"), str):
                normalized["description"] = item["description"][:1_000]
            if isinstance(item.get("context_window_tokens"), int):
                normalized["contextWindowTokens"] = item["context_window_tokens"]
            if isinstance(item.get("rate_multiplier"), (int, float)):
                normalized["rateMultiplier"] = item["rate_multiplier"]
            if isinstance(item.get("rate_unit"), str):
                normalized["rateUnit"] = item["rate_unit"][:100]
            result.append(normalized)
        if not result:
            raise ProviderError(
                "Kiro model catalog was empty.",
                error_type="kiro_cli_error",
                status_code=502,
            )
        return result

    async def custom_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        try:
            items = await self.custom_client.get_models(force=force)
        except CustomAPIError as exc:
            raise ProviderError(
                str(exc), error_type="custom_api_error", status_code=502
            ) from exc
        return self.safe_model_catalog(items)

    async def direct_models(
        self, platform_id: str | None = None, *, force: bool = False
    ) -> list[dict[str, Any]]:
        try:
            items = await self.direct_client.get_models(platform_id, force=force)
        except DirectAPIError as exc:
            raise ProviderError(
                str(exc), error_type="direct_api_error", status_code=502
            ) from exc
        return self.safe_model_catalog(items)

    async def test_direct(self, platform_id: str) -> dict[str, Any]:
        platform = direct_platform(platform_id)
        try:
            credential = await self.oauth.resolve(platform_id)
            models = await self.direct_client.get_models(platform_id, force=True)
        except (DirectAPIError, OAuthError, CredentialStoreError) as exc:
            raise ProviderError(
                str(exc), error_type="direct_auth_error", status_code=502
            ) from exc
        return {
            "ok": True,
            "platform_id": platform_id,
            "platform_name": platform.name,
            "credential_source": credential.source,
            "models": self.safe_model_catalog(models),
        }

    @staticmethod
    def direct_platforms() -> list[dict[str, object]]:
        return direct_platform_catalog()

    def set_direct_api_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {"platform_id", "api_key"}
        if unknown:
            raise ValueError(f"Unknown credential field: {sorted(unknown)[0]}")
        platform_id = payload.get("platform_id")
        api_key = payload.get("api_key")
        if not isinstance(platform_id, str) or not isinstance(api_key, str):
            raise ValueError("platform_id and api_key are required strings.")
        self.oauth.set_api_key(platform_id, api_key)
        return self.control_state()

    def logout_direct(self, platform_id: str) -> dict[str, Any]:
        self.oauth.logout(platform_id)
        return self.control_state()

    async def import_direct_credentials(
        self, platform_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"source"} or not isinstance(payload.get("source"), str):
            raise ValueError("Credential import requires one string source field.")
        try:
            await self.oauth.import_credentials(platform_id, payload["source"])
        except OAuthError as exc:
            raise ProviderError(
                str(exc), error_type="direct_auth_error", status_code=400
            ) from exc
        return self.control_state()

    async def start_direct_login(
        self,
        platform_id: str,
        payload: dict[str, Any],
        *,
        callback_base_url: str,
    ) -> dict[str, Any]:
        try:
            return await self.oauth.start(
                platform_id, payload, callback_base_url=callback_base_url
            )
        except OAuthError as exc:
            raise ProviderError(
                str(exc), error_type="direct_auth_error", status_code=400
            ) from exc

    def direct_login_status(self, session_id: str) -> dict[str, Any]:
        return self.oauth.status(session_id)

    def respond_direct_login(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"value"} or not isinstance(payload.get("value"), str):
            raise ValueError("OAuth response requires one string value field.")
        return self.oauth.respond(session_id, payload["value"])

    async def cancel_direct_login(self, session_id: str) -> dict[str, Any]:
        return await self.oauth.cancel(session_id)

    def receive_direct_callback(
        self, session_id: str, parameters: dict[str, str]
    ) -> dict[str, Any]:
        return self.oauth.receive_callback(session_id, parameters)

    async def quota(self, provider_id: str, *, force: bool = False) -> dict[str, Any]:
        if provider_id not in PROVIDER_IDS:
            raise ProviderError(
                "Unknown provider.", error_type="invalid_request_error", status_code=404
            )
        self._ensure_ready(provider_id)
        try:
            if provider_id == "kiro":
                value = await self.kiro_runner.usage(force=force)
            elif provider_id == "cursor":
                value = await self.cursor_provider.quota(force=force)
            elif provider_id == "custom":
                value = await self.custom_client.quota()
            else:
                value = await self.direct_client.quota()
                if self.direct_provider.last_usage:
                    value = {
                        **value,
                        "last_run_usage": dict(self.direct_provider.last_usage),
                    }
        except KiroInvocationError as exc:
            raise ProviderError(
                str(exc), error_type="kiro_cli_error", status_code=502
            ) from exc
        except CursorBackendError as exc:
            raise ProviderError(
                str(exc),
                error_type=f"cursor_{self.cursor_provider.backend_id()}_error",
                status_code=502,
            ) from exc
        except CustomAPIError as exc:
            raise ProviderError(
                str(exc), error_type="custom_api_error", status_code=502
            ) from exc
        except DirectAPIError as exc:
            raise ProviderError(
                str(exc), error_type="direct_api_error", status_code=502
            ) from exc
        return {
            "provider": provider_id,
            "fetched_at": datetime.now(UTC).isoformat(),
            **value,
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.update_from_api(payload)
        return self.control_state()

    def enable_codex_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {"confirmation", "model"}
        if unknown:
            raise ValueError(f"Unknown Codex config field: {sorted(unknown)[0]}")
        model = payload.get("model") or self.active_provider().model_id()
        return self.codex_config.enable(
            confirmation=payload.get("confirmation"), model=model
        )

    def disable_codex_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {"confirmation"}
        if unknown:
            raise ValueError(f"Unknown Codex config field: {sorted(unknown)[0]}")
        return self.codex_config.disable(confirmation=payload.get("confirmation"))

    def control_state(self) -> dict[str, Any]:
        executable = shutil.which(self.settings.kiro_cli)
        cursor_executable = shutil.which(self.settings.cursor_cli)
        safe_settings = self.store.safe_view()
        cursor_backend = self.cursor_provider.backend_id()
        cursor_ready = bool(
            safe_settings["cursor"]["api_key_configured"]
            and (cursor_backend == "cloud_api" or cursor_executable)
        )
        custom_ready = bool(
            safe_settings["custom"]["api_key_configured"]
            and safe_settings["custom"]["base_url"]
        )
        try:
            credential_state = self.credentials.safe_view()
        except CredentialStoreError:
            credential_state = {
                "path": str(self.credentials.path),
                "permissions": "0600",
                "encrypted": False,
                "providers": {},
                "error": "credential_store_unavailable",
            }
        direct_settings = safe_settings["direct"]
        direct_platform_id = str(direct_settings["platform_id"])
        direct_credential = credential_state["providers"].get(direct_platform_id, {})
        direct_ready = bool(direct_credential.get("configured"))
        active = str(safe_settings["active_provider"])
        readiness = {
            "kiro": bool(executable),
            "cursor": cursor_ready,
            "custom": custom_ready,
            "direct": direct_ready,
        }
        return {
            "version": __version__,
            "status": "ok" if readiness[active] else "degraded",
            "settings": safe_settings,
            "providers": {
                "kiro": {
                    "available": bool(executable),
                    "model": safe_settings["kiro"]["model_id"],
                    "max_concurrency": self.settings.kiro_max_concurrency,
                },
                "cursor": {
                    "configured": cursor_ready,
                    "backend": cursor_backend,
                    "max_concurrency": self.settings.cursor_max_concurrency,
                    "cli_available": bool(cursor_executable),
                    "cli_path": cursor_executable,
                    "base_url": safe_settings["cursor"]["base_url"],
                },
                "custom": {
                    "configured": custom_ready,
                    "base_url": safe_settings["custom"]["base_url"],
                    "model": safe_settings["custom"]["model_id"] or "custom-default",
                },
                "direct": {
                    "configured": direct_ready,
                    "platform_id": direct_platform_id,
                    "model": direct_settings["model_id"],
                    "max_concurrency": self.settings.direct_max_concurrency,
                    "stability": next(
                        (
                            item["stability"]
                            for item in direct_platform_catalog()
                            if item["id"] == direct_platform_id
                        ),
                        "unknown",
                    ),
                },
            },
            "direct_platforms": direct_platform_catalog(),
            "credentials": credential_state,
            "codex_config": self.codex_config.status(),
            "log_history": {
                "path": str(
                    self.settings.log_path
                    or default_data_dir() / "logs" / "switchboard.log"
                ),
                "max_bytes": self.settings.log_max_bytes,
                "backup_count": self.settings.log_backup_count,
                "redacted": True,
            },
            "session_reuse": self.settings.session_reuse,
            "session_ttl_seconds": self.settings.session_ttl_seconds,
            "last_upstream_request": self.inspector.snapshot(),
        }

    def health(self) -> dict[str, Any]:
        state = self.control_state()
        return {
            "status": state["status"],
            "version": state["version"],
            "active_provider": state["settings"]["active_provider"],
            "kiro_available": state["providers"]["kiro"]["available"],
            "kiro_model": state["providers"]["kiro"]["model"],
            "kiro_max_concurrency": state["providers"]["kiro"]["max_concurrency"],
            "cursor_configured": state["providers"]["cursor"]["configured"],
            "cursor_backend": state["providers"]["cursor"]["backend"],
            "cursor_max_concurrency": state["providers"]["cursor"]["max_concurrency"],
            "cursor_cli_available": state["providers"]["cursor"]["cli_available"],
            "custom_configured": state["providers"]["custom"]["configured"],
            "direct_configured": state["providers"]["direct"]["configured"],
            "direct_platform": state["providers"]["direct"]["platform_id"],
            "session_reuse": state["session_reuse"],
            "session_ttl_seconds": state["session_ttl_seconds"],
        }

    def models(self) -> dict[str, Any]:
        provider_id = self.active_provider_id()
        model_id = self.providers[provider_id].model_id()
        return {
            "object": "list",
            # Codex probes a private catalog shape while OpenAI-compatible clients
            # expect `data`. An empty private catalog makes Codex retain its bundled
            # model metadata without breaking the public Models API response.
            "models": [],
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": f"{provider_id}-provider-switchboard",
                }
            ],
        }
