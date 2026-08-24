from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from .. import __version__
from ..compatibility.profiles import ProviderCapabilities, compatibility_profile
from ..domain.agent_loop_guard import AgentControlLoop, detect_agent_control_loop
from ..domain.bridge import (
    BridgeResult,
    encode_sse,
    output_items,
    request_summary,
    response_object,
    streaming_events,
)
from ..infrastructure.codex_config import CodexConfigManager
from ..infrastructure.config_store import PROVIDER_IDS, ConfigStore
from ..infrastructure.credential_store import CredentialStore, CredentialStoreError
from ..infrastructure.cursor_client import CursorBackendError
from ..infrastructure.custom_client import CustomAPIError, CustomResponsesClient
from ..infrastructure.direct_catalog import direct_platform, direct_platform_catalog
from ..infrastructure.direct_client import DirectAPIError, DirectClient
from ..infrastructure.kiro_cli import KiroInvocationError, KiroRunner
from ..infrastructure.oauth import OAuthError, OAuthLoginManager
from ..infrastructure.pi_credentials import (
    PiCredentialCandidate,
    PiCredentialImporter,
    PiCredentialImportError,
    PiCredentialScan,
)
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
        pi_credentials: PiCredentialImporter,
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
        self.pi_credentials = pi_credentials
        self.codex_config = codex_config
        self.inspector = inspector
        self.providers = providers

    def active_provider_id(self) -> str:
        provider = self.store.read().get("active_provider")
        return provider if provider in PROVIDER_IDS else "kiro"

    def active_provider(self) -> ResponsesProvider:
        return self.providers[self.active_provider_id()]

    def _provider_capabilities(self, provider_id: str) -> ProviderCapabilities:
        if provider_id == "direct":
            platform_id = str(self.store.read()["direct"]["platform_id"])
            profile_id = direct_platform(platform_id).compatibility_profile
        elif provider_id == "custom":
            profile_id = str(
                self.store.read()["custom"].get("compatibility_profile")
                or "function_only"
            )
        else:
            profile_id = "prompt_bridge"
        return compatibility_profile(profile_id)

    def _validate_native_remote_compaction(
        self, body: dict[str, Any], provider_id: str
    ) -> None:
        input_value = body.get("input")
        if not isinstance(input_value, list) or not any(
            isinstance(item, dict) and item.get("type") == "compaction_trigger"
            for item in input_value
        ):
            return
        if self._provider_capabilities(provider_id).native_compaction:
            return
        logger.warning("Rejected unsupported native remote compaction request")
        raise ProviderError(
            "The selected provider cannot execute native Responses compaction. "
            "Switch to a native Codex-compatible provider or start a new task.",
            error_type="unsupported_feature",
            status_code=400,
        )

    def _log_request(self, body: dict[str, Any], provider_id: str) -> None:
        if not self.settings.debug_requests:
            return
        summary = request_summary(body)
        summary["provider"] = provider_id
        logger.info("request_metadata=%s", json.dumps(summary, ensure_ascii=True))

    def _agent_loop_decision(
        self, body: dict[str, Any], provider_id: str
    ) -> AgentControlLoop | None:
        if not self.settings.agent_loop_guard:
            return None
        multi_agent = body.get("multi_agent")
        if (
            self._provider_capabilities(provider_id).native_multi_agent
            and isinstance(multi_agent, dict)
            and multi_agent.get("enabled") is True
        ):
            return None
        decision = detect_agent_control_loop(
            body,
            restart_limit=self.settings.agent_loop_restart_limit,
        )
        if decision is None:
            return None
        model = self._guard_model(body, provider_id)
        self.inspector.record(
            provider=provider_id,
            action="agent_control_loop_stopped",
            model=model,
            effort=None,
            session_reused=False,
        )
        logger.warning(
            "Stopped repeated subagent control loop provider=%s reason=%s "
            "control_calls=%d restart_count=%d target=%s",
            provider_id,
            decision.reason,
            decision.control_calls,
            decision.restart_count,
            decision.target_digest or "none",
        )
        return decision

    def _guard_model(self, body: dict[str, Any], provider_id: str) -> str:
        requested = body.get("model")
        if isinstance(requested, str) and requested:
            return requested
        try:
            return self.providers[provider_id].model_id()
        except (KeyError, TypeError, ValueError):
            return "unknown"

    def _guard_response(
        self,
        body: dict[str, Any],
        provider_id: str,
        decision: AgentControlLoop,
    ) -> ProviderResponse:
        model = self._guard_model(body, provider_id)
        response = response_object(
            body,
            model,
            f"resp_{uuid.uuid4().hex}",
            "completed",
            output_items(BridgeResult(text=decision.user_message)),
            None,
        )
        return ProviderResponse(
            response,
            {
                "X-Switchboard-Provider": provider_id,
                "X-Switchboard-Guard": "agent-control-loop",
            },
        )

    async def _guard_stream(
        self,
        body: dict[str, Any],
        provider_id: str,
        decision: AgentControlLoop,
    ) -> AsyncIterator[bytes]:
        events, _completed = streaming_events(
            body,
            self._guard_model(body, provider_id),
            BridgeResult(text=decision.user_message),
            "",
        )
        for chunk in encode_sse(events):
            yield chunk

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
        self._validate_native_remote_compaction(body, provider_id)
        self._log_request(body, provider_id)
        decision = self._agent_loop_decision(body, provider_id)
        if decision is not None:
            return self._guard_response(body, provider_id, decision)
        self._ensure_ready(provider_id)
        return await self.providers[provider_id].complete(body)

    async def compact(self, body: dict[str, Any]) -> ProviderResponse:
        provider_id = self.active_provider_id()
        capabilities = self._provider_capabilities(provider_id)
        if not capabilities.native_compaction:
            raise ProviderError(
                "The selected provider does not support native Responses compaction.",
                error_type="unsupported_feature",
                status_code=400,
            )
        self._ensure_ready(provider_id)
        try:
            if provider_id == "direct":
                value = await self.direct_client.compact_responses(body)
                platform_id = str(self.store.read()["direct"]["platform_id"])
                return ProviderResponse(
                    value,
                    {
                        "X-Switchboard-Provider": provider_id,
                        "X-Switchboard-Platform": platform_id,
                    },
                )
            if provider_id == "custom":
                value = await self.custom_client.compact_response(body)
                return ProviderResponse(value, {"X-Switchboard-Provider": provider_id})
        except (DirectAPIError, CustomAPIError) as exc:
            status = exc.status_code
            raise ProviderError(
                str(exc),
                error_type="compaction_error",
                status_code=(status if isinstance(status, int) else 502),
            ) from exc
        raise ProviderError(
            "The selected provider cannot route native Responses compaction.",
            error_type="unsupported_feature",
            status_code=400,
        )

    def stream(self, body: dict[str, Any]) -> tuple[str, AsyncIterator[bytes]]:
        provider_id = self.active_provider_id()
        return provider_id, self.stream_for(provider_id, body)

    def stream_for(
        self,
        provider_id: str,
        body: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """Start a stream with the provider selected when a request was accepted."""
        if provider_id not in self.providers:
            raise ProviderError(
                "Selected provider is no longer available.",
                error_type="provider_configuration_error",
                status_code=503,
            )
        self._validate_native_remote_compaction(body, provider_id)
        self._log_request(body, provider_id)
        decision = self._agent_loop_decision(body, provider_id)
        if decision is not None:
            return self._guard_stream(body, provider_id, decision)
        self._ensure_ready(provider_id)
        return self.providers[provider_id].stream(body)

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

    def _scan_pi_credentials(self) -> PiCredentialScan:
        try:
            return self.pi_credentials.scan()
        except PiCredentialImportError as exc:
            raise ProviderError(
                str(exc), error_type="credential_import_error", status_code=400
            ) from exc

    def preview_pi_credentials(self) -> dict[str, object]:
        scan = self._scan_pi_credentials()
        direct_status = self.credentials.safe_status()
        cursor = self.store.safe_view()["cursor"]
        candidates: list[dict[str, object]] = []
        for candidate in scan.candidates:
            item: dict[str, object] = candidate.safe_view()
            if candidate.target_kind == "direct":
                status = direct_status[candidate.target_id]
                item["configured"] = bool(status["configured"])
                item["configured_source"] = str(status["source"])
            else:
                item["configured"] = bool(cursor["api_key_configured"])
                item["configured_source"] = str(cursor["api_key_source"])
            candidates.append(item)
        return {
            "source": "pi",
            "path": "~/.pi/agent/auth.json",
            "available": True,
            "candidates": candidates,
            "unsupported": [dict(item) for item in scan.unsupported],
        }

    async def _apply_pi_candidate(
        self, candidate: PiCredentialCandidate, *, replace_existing: bool
    ) -> str | None:
        if candidate.target_kind == "cursor":
            cursor = self.store.safe_view()["cursor"]
            source = str(cursor["api_key_source"])
            if source == "environment":
                return "CURSOR_API_KEY environment override is active"
            if bool(cursor["api_key_configured"]) and not replace_existing:
                return "target already has a credential"
            if candidate.api_key is None:
                return "Pi Cursor record does not contain a stored API key"
            self.store.update_from_api({"cursor": {"api_key": candidate.api_key}})
            return None

        status = self.credentials.safe_status()[candidate.target_id]
        source = str(status["source"])
        if source.startswith("env:"):
            return f"{source} environment override is active"
        if bool(status["configured"]) and not replace_existing:
            return "target already has a credential"
        await self.oauth.cancel_platform_logins(candidate.target_id)
        if candidate.credential_type == "api_key":
            if candidate.api_key is None:
                return "Pi record does not contain a stored API key"
            self.credentials.set_api_key(candidate.target_id, candidate.api_key)
            return None
        if (
            candidate.access is None
            or candidate.refresh is None
            or candidate.expires_at is None
        ):
            return "Pi OAuth record is incomplete"
        self.credentials.set_oauth(
            candidate.target_id,
            access=candidate.access,
            refresh=candidate.refresh,
            expires_at=candidate.expires_at,
            extra=candidate.extra,
        )
        return None

    async def import_pi_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"replace_existing"}:
            raise ValueError("Unknown Pi import field.")
        replace_existing = payload.get("replace_existing", False)
        if not isinstance(replace_existing, bool):
            raise ValueError("replace_existing must be a boolean.")
        scan = self._scan_pi_credentials()
        imported: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for candidate in scan.candidates:
            item = candidate.safe_view()
            try:
                reason = await self._apply_pi_candidate(
                    candidate, replace_existing=replace_existing
                )
            except (CredentialStoreError, OSError, ValueError):
                logger.warning(
                    "Pi credential import failed source=%s target_kind=%s target=%s",
                    candidate.source_provider,
                    candidate.target_kind,
                    candidate.target_id,
                )
                skipped.append(
                    {**item, "reason": "credential could not be stored safely"}
                )
                continue
            if reason is None:
                imported.append(item)
            else:
                skipped.append({**item, "reason": reason})
        return {
            "source": "pi",
            "imported": imported,
            "skipped": skipped,
            "unsupported": [dict(item) for item in scan.unsupported],
            "state": self.control_state(),
        }

    async def import_direct_credentials(
        self, platform_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"source"} or not isinstance(payload.get("source"), str):
            raise ValueError("Credential import requires one string source field.")
        if platform_id != "kiro_direct" or payload["source"] != "pi":
            raise ValueError("This credential import source is not supported.")
        candidate = next(
            (
                item
                for item in self._scan_pi_credentials().candidates
                if item.target_kind == "direct" and item.target_id == platform_id
            ),
            None,
        )
        if candidate is None:
            raise ProviderError(
                "Pi does not contain an importable Kiro credential.",
                error_type="direct_auth_error",
                status_code=400,
            )
        try:
            reason = await self._apply_pi_candidate(candidate, replace_existing=True)
        except (CredentialStoreError, OSError, ValueError) as exc:
            raise ProviderError(
                "The Pi Kiro credential could not be stored safely.",
                error_type="direct_auth_error",
                status_code=400,
            ) from exc
        if reason is not None:
            raise ProviderError(reason, error_type="direct_auth_error", status_code=400)
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

    @staticmethod
    def _codex_agent_options(
        payload: dict[str, Any], *, required: bool
    ) -> tuple[bool | None, int | None]:
        mode = payload.get("agent_mode")
        if mode is None and not required:
            if payload.get("agent_max_threads") is not None:
                raise ValueError("agent_max_threads requires agent_mode.")
            return None, None
        if mode not in {"single", "limited", "parallel", "custom"}:
            raise ValueError("agent_mode must be single, limited, parallel, or custom.")
        requested_threads = payload.get("agent_max_threads")
        if mode == "single":
            if requested_threads is not None:
                raise ValueError("single agent mode does not accept a thread limit.")
            return False, None
        if mode == "limited":
            if requested_threads is not None:
                raise ValueError("limited agent mode uses a fixed thread limit.")
            return True, 2
        if mode == "parallel":
            if requested_threads is not None:
                raise ValueError("parallel agent mode uses a fixed thread limit.")
            return True, 4
        if (
            not isinstance(requested_threads, int)
            or isinstance(requested_threads, bool)
            or not 1 <= requested_threads <= 16
        ):
            raise ValueError("custom agent threads must be an integer from 1 to 16.")
        return True, requested_threads

    def enable_codex_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {
            "confirmation",
            "model",
            "agent_mode",
            "agent_max_threads",
        }
        if unknown:
            raise ValueError(f"Unknown Codex config field: {sorted(unknown)[0]}")
        model = payload.get("model") or self.active_provider().model_id()
        agents_enabled, max_agent_threads = self._codex_agent_options(
            payload, required=False
        )
        return self.codex_config.enable(
            confirmation=payload.get("confirmation"),
            model=model,
            agents_enabled=agents_enabled,
            max_agent_threads=max_agent_threads,
        )

    def configure_codex_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {
            "confirmation",
            "agent_mode",
            "agent_max_threads",
        }
        if unknown:
            raise ValueError(f"Unknown Codex config field: {sorted(unknown)[0]}")
        agents_enabled, max_agent_threads = self._codex_agent_options(
            payload, required=True
        )
        return self.codex_config.configure_agents(
            confirmation=payload.get("confirmation"),
            agents_enabled=agents_enabled,
            max_agent_threads=max_agent_threads,
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
