from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from .application.inspector import RequestInspector
from .application.service import SwitchboardService
from .infrastructure.codex_config import CodexConfigManager
from .infrastructure.config_store import ConfigStore
from .infrastructure.credential_store import CredentialStore
from .infrastructure.cursor_cli import CursorCliRunner
from .infrastructure.cursor_client import CursorClient
from .infrastructure.custom_client import CustomResponsesClient
from .infrastructure.direct_client import DirectClient
from .infrastructure.kiro_cli import KiroRunner
from .infrastructure.oauth import OAuthLoginManager
from .infrastructure.session_cache import SessionCache
from .providers.cursor import CursorProvider
from .providers.custom import CustomProvider
from .providers.direct import DirectProvider
from .providers.kiro import KiroProvider
from .settings import AppSettings


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: AppSettings
    store: ConfigStore
    inspector: RequestInspector
    kiro_runner: KiroRunner
    cursor_client: CursorClient
    cursor_cli_runner: CursorCliRunner
    custom_client: CustomResponsesClient
    credentials: CredentialStore
    oauth: OAuthLoginManager
    direct_client: DirectClient
    codex_config: CodexConfigManager
    kiro_provider: KiroProvider
    cursor_provider: CursorProvider
    custom_provider: CustomProvider
    direct_provider: DirectProvider
    service: SwitchboardService


def build_runtime(
    *,
    settings: AppSettings | None = None,
    config_path: Path | None = None,
    cursor_transport: httpx.AsyncBaseTransport | None = None,
    custom_transport: httpx.AsyncBaseTransport | None = None,
    direct_transport: httpx.AsyncBaseTransport | None = None,
    oauth_transport: httpx.AsyncBaseTransport | None = None,
    kiro_runner: KiroRunner | None = None,
    codex_config_path: Path | None = None,
) -> Runtime:
    resolved_settings = settings or AppSettings.from_env()
    store = ConfigStore(config_path, kiro_model=resolved_settings.kiro_model)
    inspector = RequestInspector()
    runner = kiro_runner or KiroRunner(resolved_settings)
    cursor_client = CursorClient(store, transport=cursor_transport)
    cursor_cli_runner = CursorCliRunner(resolved_settings, store)
    custom_client = CustomResponsesClient(store, transport=custom_transport)
    credentials = CredentialStore(store.path.parent / "credentials.json")
    oauth = OAuthLoginManager(
        resolved_settings,
        credentials,
        transport=oauth_transport or direct_transport,
    )
    direct_client = DirectClient(
        resolved_settings,
        store,
        oauth,
        transport=direct_transport,
    )
    codex_config = CodexConfigManager(
        resolved_settings,
        store.runtime_dir / "codex-config-state.json",
        config_path=codex_config_path,
    )
    kiro_cache = SessionCache(
        resolved_settings.kiro_workdir,
        enabled=resolved_settings.session_reuse,
        ttl_seconds=resolved_settings.session_ttl_seconds,
    )
    cursor_cache = SessionCache(
        store.runtime_dir / "cursor",
        enabled=resolved_settings.session_reuse,
        ttl_seconds=resolved_settings.session_ttl_seconds,
    )
    kiro_provider = KiroProvider(
        resolved_settings, store, runner, kiro_cache, inspector
    )
    cursor_provider = CursorProvider(
        store, cursor_client, cursor_cli_runner, cursor_cache, inspector
    )
    custom_provider = CustomProvider(store, custom_client, inspector)
    direct_provider = DirectProvider(store, direct_client, inspector)
    service = SwitchboardService(
        resolved_settings,
        store,
        runner,
        cursor_provider,
        custom_client,
        direct_client,
        direct_provider,
        credentials,
        oauth,
        codex_config,
        inspector,
        {
            "kiro": kiro_provider,
            "cursor": cursor_provider,
            "custom": custom_provider,
            "direct": direct_provider,
        },
    )
    return Runtime(
        settings=resolved_settings,
        store=store,
        inspector=inspector,
        kiro_runner=runner,
        cursor_client=cursor_client,
        cursor_cli_runner=cursor_cli_runner,
        custom_client=custom_client,
        credentials=credentials,
        oauth=oauth,
        direct_client=direct_client,
        codex_config=codex_config,
        kiro_provider=kiro_provider,
        cursor_provider=cursor_provider,
        custom_provider=custom_provider,
        direct_provider=direct_provider,
        service=service,
    )
