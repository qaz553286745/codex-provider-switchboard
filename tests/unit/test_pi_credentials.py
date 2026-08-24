from __future__ import annotations

import json

import pytest

from codex_provider_switchboard.infrastructure.pi_credentials import (
    PiCredentialImporter,
    PiCredentialImportError,
)


def _write_auth(path, value: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def test_scan_maps_supported_pi_credentials_without_exposing_secrets(tmp_path) -> None:
    path = tmp_path / "agent" / "auth.json"
    _write_auth(
        path,
        {
            "openai-codex": {
                "type": "oauth",
                "access": "test-openai-access",
                "refresh": "test-openai-refresh",
                "expires": 4_102_444_800_000,
                "accountId": "acct-test",
            },
            "kiro": {
                "type": "oauth",
                "access": "test-kiro-access",
                "refresh": "test-kiro-refresh|test-client|test-secret|idc",
                "expires": 4_102_444_800_000,
                "clientId": "test-client",
                "clientSecret": "test-secret",
                "region": "us-east-1",
                "authMethod": "idc",
            },
            "anthropic": {"type": "api_key", "key": "test-anthropic-key"},
            "cursor": {"type": "api_key", "key": "test-cursor-key"},
            "github-copilot": {
                "type": "oauth",
                "access": "tid=test;proxy-ep=proxy.individual.githubcopilot.com",
                "refresh": "test-github-token",
                "expires": 4_102_444_800_000,
            },
            "unknown-provider": {"type": "api_key", "key": "test-unknown"},
        },
    )

    scan = PiCredentialImporter(path).scan()

    mapped = {
        (item.source_provider, item.target_kind, item.target_id)
        for item in scan.candidates
    }
    assert mapped == {
        ("anthropic", "direct", "anthropic"),
        ("cursor", "cursor", "cursor"),
        ("github-copilot", "direct", "github_copilot"),
        ("kiro", "direct", "kiro_direct"),
        ("openai-codex", "direct", "openai_codex"),
    }
    assert scan.unsupported == (
        {
            "source_provider": "unsupported",
            "reason": "no compatible Switchboard provider",
        },
    )
    safe = json.dumps(scan.safe_view())
    rendered = repr(scan.candidates)
    assert "unknown-provider" not in safe
    for secret in (
        "test-openai-access",
        "test-openai-refresh",
        "test-kiro-access",
        "test-kiro-refresh",
        "test-anthropic-key",
        "test-cursor-key",
        "test-github-token",
    ):
        assert secret not in safe
        assert secret not in rendered

    kiro = next(item for item in scan.candidates if item.target_id == "kiro_direct")
    assert kiro.refresh == "test-kiro-refresh"
    assert kiro.extra["auth_method"] == "idc"
    copilot = next(
        item for item in scan.candidates if item.target_id == "github_copilot"
    )
    assert copilot.extra["base_url"] == "https://api.individual.githubcopilot.com"


def test_scan_rejects_unsafe_pi_auth_files(tmp_path) -> None:
    public = tmp_path / "public.json"
    _write_auth(public, {}, mode=0o644)
    with pytest.raises(PiCredentialImportError, match="other users"):
        PiCredentialImporter(public).scan()

    target = tmp_path / "target.json"
    _write_auth(target, {})
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(PiCredentialImportError, match="symbolic link"):
        PiCredentialImporter(linked).scan()

    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * (512 * 1_024 + 1), encoding="utf-8")
    oversized.chmod(0o600)
    with pytest.raises(PiCredentialImportError, match="size limit"):
        PiCredentialImporter(oversized).scan()


def test_scan_skips_incompatible_credential_types(tmp_path) -> None:
    path = tmp_path / "auth.json"
    _write_auth(
        path,
        {
            "openai-codex": {"type": "api_key", "key": "test-key"},
            "cursor": {
                "type": "oauth",
                "access": "test-access",
                "refresh": "test-refresh",
                "expires": 4_102_444_800_000,
            },
        },
    )

    scan = PiCredentialImporter(path).scan()

    assert scan.candidates == ()
    assert {item["source_provider"] for item in scan.unsupported} == {
        "cursor",
        "openai-codex",
    }


@pytest.mark.parametrize(
    ("source_provider", "target_kind", "target_id"),
    (
        ("openai", "direct", "openai"),
        ("anthropic", "direct", "anthropic"),
        ("xai", "direct", "xai"),
        ("openrouter", "direct", "openrouter"),
        ("cursor", "cursor", "cursor"),
        ("cursor-agent", "cursor", "cursor"),
    ),
)
def test_scan_maps_every_supported_api_key_provider(
    tmp_path, source_provider: str, target_kind: str, target_id: str
) -> None:
    path = tmp_path / source_provider / "auth.json"
    _write_auth(path, {source_provider: {"type": "api_key", "key": "test-key"}})

    scan = PiCredentialImporter(path).scan()

    assert len(scan.candidates) == 1
    candidate = scan.candidates[0]
    assert (candidate.target_kind, candidate.target_id) == (target_kind, target_id)
    assert scan.unsupported == ()


def test_scan_maps_supported_subscription_oauth_and_rejects_duplicate_target(
    tmp_path,
) -> None:
    path = tmp_path / "auth.json"
    oauth = {
        "type": "oauth",
        "access": "test-access",
        "refresh": "test-refresh",
        "expires": 4_102_444_800_000,
    }
    _write_auth(
        path,
        {
            "anthropic": oauth,
            "xai": oauth,
            "openrouter": oauth,
            "cursor": {"type": "api_key", "key": "test-cursor-key"},
            "cursor-agent": {
                "type": "api_key",
                "key": "test-second-cursor-key",
            },
        },
    )

    scan = PiCredentialImporter(path).scan()

    assert {
        (candidate.source_provider, candidate.target_id)
        for candidate in scan.candidates
    } == {
        ("anthropic", "anthropic"),
        ("cursor", "cursor"),
        ("openrouter", "openrouter"),
        ("xai", "xai"),
    }
    assert scan.unsupported == (
        {
            "source_provider": "cursor-agent",
            "reason": "another Pi record already maps to this target",
        },
    )
