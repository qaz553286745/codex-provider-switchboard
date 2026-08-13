import json
import stat

import pytest

from codex_provider_switchboard.infrastructure.config_store import ConfigStore


def test_config_store_saves_secret_atomically_without_returning_it(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    path = tmp_path / "settings" / "config.json"
    store = ConfigStore(path)

    store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {
                "api_key": "test_cursor_key",
                "model_id": "gpt-test",
                "model_params": [{"id": "reasoning_effort", "value": "max"}],
                "model_display_name": "GPT Test · Max",
            },
        }
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["cursor"]["api_key"] == "test_cursor_key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    safe = store.safe_view()
    assert "api_key" not in safe["cursor"]
    assert safe["cursor"]["api_key_configured"] is True
    assert safe["cursor"]["backend"] == "cli"
    assert safe["active_provider"] == "cursor"

    store.update_from_api({"cursor": {"api_key": ""}})
    assert store.api_key() == "test_cursor_key"
    store.update_from_api({"cursor": {"clear_api_key": True}})
    assert store.api_key() == ""


def test_config_store_rejects_key_exfiltration_base_url(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(ValueError, match=r"api\.cursor\.com"):
        store.update_from_api(
            {"cursor": {"base_url": "https://example.invalid/collect"}}
        )


def test_cursor_cloud_api_backend_is_explicit_opt_in(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"cursor": {"backend": "cloud_api"}})
    assert store.read()["cursor"]["backend"] == "cloud_api"
    with pytest.raises(ValueError, match="cli or cloud_api"):
        store.update_from_api({"cursor": {"backend": "unknown"}})
