from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import pytest

from codex_provider_switchboard.infrastructure.codex_config import (
    CodexConfigError,
    CodexConfigManager,
)
from codex_provider_switchboard.settings import AppSettings


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        host="127.0.0.1",
        port=8787,
        token=None,
        max_request_bytes=1_048_576,
        debug_requests=False,
        session_reuse=True,
        session_ttl_seconds=3_600,
        kiro_cli="kiro-cli",
        kiro_model="gpt-5.6-sol",
        kiro_workdir=tmp_path / "kiro",
        kiro_timeout_seconds=30,
        kiro_max_concurrency=1,
        kiro_max_prompt_bytes=1_048_576,
        kiro_context_recovery_prompt_bytes=512 * 1_024,
        kiro_max_output_bytes=1_048_576,
        kiro_allow_requested_model=False,
        kiro_tool_batching=True,
        cursor_cli="cursor-agent",
        cursor_workdir=tmp_path / "cursor",
        cursor_max_concurrency=1,
        cursor_max_prompt_bytes=1_048_576,
        cursor_max_output_bytes=1_048_576,
    )


def test_codex_config_preserves_existing_provider_identity(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    original = b"""# keep this comment
model_provider = "company"
model = "company-model"

[model_providers.company]
name = "Company"
base_url = "https://company.example/v1"
wire_api = "responses"
env_key = "COMPANY_API_KEY"

[plugins."visualize@example"]
enabled = true
"""
    config_path.write_bytes(original)
    manager = CodexConfigManager(
        _settings(tmp_path),
        tmp_path / "state" / "codex.json",
        config_path=config_path,
    )

    enabled = manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    assert enabled["active"] is True
    assert enabled["current_matches_managed"] is True
    backup_path = Path(enabled["backup_path"])
    assert backup_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["model_provider"] == "company"
    assert parsed["model"] == "gpt-5.6-sol"
    assert parsed["plugins"]["visualize@example"]["enabled"] is True
    local = parsed["model_providers"]["company"]
    assert local["base_url"] == "http://127.0.0.1:8787/v1"
    assert local["requires_openai_auth"] is False
    assert enabled["history_provider_preserved"] is True

    second = manager.enable(confirmation="ENABLE", model="ignored-model")
    assert second["backup_path"] == str(backup_path)
    restored = manager.disable(confirmation="RESTORE")
    assert restored["active"] is False
    restored_config = tomllib.loads(config_path.read_text())
    assert restored_config == tomllib.loads(original.decode())


def test_codex_config_preserves_automatic_and_user_fields(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        'model = "old"\n'
        '\n[marketplaces.openai-bundled]\nlast_updated = "before"\n'
        '\n[desktop]\nconversationDetailMode = "STEPS"\n'
        '\n[plugins."keep@example"]\nenabled = true\n'
        "\n[features]\njs_repl = false\n"
    )
    manager = CodexConfigManager(
        _settings(tmp_path),
        tmp_path / "state.json",
        config_path=config_path,
    )
    enabled = manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    active = config_path.read_text()
    assert 'model_provider = "codex-provider-switchboard"' in active
    assert "openai_base_url" not in active
    assert enabled["managed_fields"] == [
        "model",
        "model_provider",
        "model_providers.codex-provider-switchboard",
    ]
    parsed_active = tomllib.loads(active)
    assert parsed_active["features"] == {"js_repl": False}
    provider = parsed_active["model_providers"]["codex-provider-switchboard"]
    assert provider["name"] == "Local Codex Provider Switchboard"
    assert provider["base_url"] == "http://127.0.0.1:8787/v1"
    assert provider["requires_openai_auth"] is False
    config_path.write_text(
        active.replace('last_updated = "before"', 'last_updated = "after"').replace(
            'conversationDetailMode = "STEPS"', 'conversationDetailMode = "PROSE"'
        )
        + '\n[plugins."added-automatically@example"]\nenabled = true\n'
        + "\n[features.automatically_added]\nenabled = true\n"
        + "# user edit\n"
    )

    restored = manager.disable(confirmation="RESTORE")
    parsed = tomllib.loads(config_path.read_text())
    assert restored["restore_method"] == "field_level"
    assert parsed["model"] == "old"
    assert "openai_base_url" not in parsed
    assert parsed["marketplaces"]["openai-bundled"]["last_updated"] == "after"
    assert parsed["desktop"]["conversationDetailMode"] == "PROSE"
    assert parsed["plugins"]["keep@example"]["enabled"] is True
    assert parsed["plugins"]["added-automatically@example"]["enabled"] is True
    assert parsed["features"]["js_repl"] is False
    assert "enable_request_compression" not in parsed["features"]
    assert parsed["features"]["automatically_added"]["enabled"] is True
    assert "# user edit" in config_path.read_text()


def test_codex_config_preserves_existing_request_compression_value(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        'model = "old"\n\n[features]\nenable_request_compression = true\n'
    )
    manager = CodexConfigManager(
        _settings(tmp_path),
        tmp_path / "state.json",
        config_path=config_path,
    )

    manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    assert (
        tomllib.loads(config_path.read_text())["features"]["enable_request_compression"]
        is True
    )
    manager.disable(confirmation="RESTORE")
    assert (
        tomllib.loads(config_path.read_text())["features"]["enable_request_compression"]
        is True
    )


def test_codex_config_restores_managed_fields_after_they_drift(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('model = "old"\n')
    manager = CodexConfigManager(
        _settings(tmp_path),
        tmp_path / "state.json",
        config_path=config_path,
    )
    manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    config_path.write_text(
        config_path.read_text().replace(
            'model = "gpt-5.6-sol"',
            'model = "user-selected-model"',
        )
        + '\n[desktop]\nconversationDetailMode = "PROSE"\n'
    )

    restored = manager.disable(confirmation="RESTORE")
    parsed = tomllib.loads(config_path.read_text())
    assert restored["active"] is False
    assert restored["restore_method"] == "field_level"
    assert parsed["model"] == "old"
    assert "model_provider" not in parsed
    assert "model_providers" not in parsed
    assert parsed["desktop"]["conversationDetailMode"] == "PROSE"


def test_v2_state_reconciles_manual_restore_without_backup(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    original = 'model = "old"\n\n[desktop]\nconversationDetailMode = "PROSE"\n'
    config_path.write_text(original)
    manager = CodexConfigManager(
        _settings(tmp_path), tmp_path / "state.json", config_path=config_path
    )

    enabled = manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    Path(enabled["backup_path"]).unlink()
    config_path.write_text(original)

    reconciled = manager.status()
    assert reconciled["active"] is False
    assert reconciled["restore_method"] == "external_detach"
    reenabled = manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    assert reenabled["active"] is True
    assert reenabled["backup_exists"] is True


def test_codex_config_detaches_safely_when_backup_is_missing(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('model = "old"\n')
    manager = CodexConfigManager(
        _settings(tmp_path), tmp_path / "state.json", config_path=config_path
    )

    enabled = manager.enable(confirmation="ENABLE", model="gpt-5.6-sol")
    Path(enabled["backup_path"]).unlink()
    config_path.write_text(
        config_path.read_text() + '\n[desktop]\nconversationDetailMode = "PROSE"\n'
    )

    restored = manager.disable(confirmation="RESTORE")
    parsed = tomllib.loads(config_path.read_text())
    assert restored["active"] is False
    assert restored["restore_method"] == "managed_cleanup"
    assert restored["restore_warning"] == "backup_missing_or_invalid"
    assert parsed["model"] == "gpt-5.6-sol"
    assert "model_provider" not in parsed
    assert "model_providers" not in parsed
    assert parsed["desktop"]["conversationDetailMode"] == "PROSE"


def test_legacy_state_reconciles_a_manual_restore_without_backup(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('[desktop]\nconversationDetailMode = "PROSE"\n')
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "active": True,
                "config_path": str(config_path),
                "model": "gpt-5.6-sol",
                "original_existed": True,
                "backup_path": str(tmp_path / "missing-backup.toml"),
            }
        )
    )
    manager = CodexConfigManager(
        _settings(tmp_path), state_path, config_path=config_path
    )

    status = manager.status()
    assert status["active"] is False
    assert status["restore_method"] == "external"
    assert 'conversationDetailMode = "PROSE"' in config_path.read_text()


def test_codex_config_requires_explicit_confirmation(tmp_path) -> None:
    manager = CodexConfigManager(
        _settings(tmp_path),
        tmp_path / "state.json",
        config_path=tmp_path / ".codex" / "config.toml",
    )
    with pytest.raises(CodexConfigError, match="ENABLE"):
        manager.enable(confirmation="yes", model="gpt-5.6-sol")
    assert not manager.config_path.exists()
