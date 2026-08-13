from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

DirectProtocol = Literal["responses", "anthropic", "kiro"]
DirectStability = Literal["stable", "beta", "experimental"]


@dataclass(frozen=True, slots=True)
class DirectPlatform:
    """Non-secret, allow-listed metadata for one directly connected platform."""

    id: str
    name: str
    protocol: DirectProtocol
    base_url: str
    response_path: str
    models_path: str | None
    auth_modes: tuple[str, ...]
    env_names: tuple[str, ...]
    default_model: str
    stability: DirectStability
    subscription: bool = False
    note: str = ""

    def safe_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["auth_modes"] = list(self.auth_modes)
        value["env_names"] = list(self.env_names)
        return value


_PLATFORMS = (
    DirectPlatform(
        id="openai",
        name="OpenAI API",
        protocol="responses",
        base_url="https://api.openai.com/v1",
        response_path="/responses",
        models_path="/models",
        auth_modes=("api_key",),
        env_names=("OPENAI_API_KEY",),
        default_model="gpt-5.6-sol",
        stability="stable",
        note="Official OpenAI Responses API using an API key.",
    ),
    DirectPlatform(
        id="openai_codex",
        name="ChatGPT Codex",
        protocol="responses",
        base_url="https://chatgpt.com/backend-api",
        response_path="/codex/responses",
        models_path=None,
        auth_modes=("oauth",),
        env_names=(),
        default_model="gpt-5.6-sol",
        stability="experimental",
        subscription=True,
        note=(
            "Uses ChatGPT account OAuth and the Codex backend. The login is official "
            "Codex OAuth, but this Switchboard adapter is not an OpenAI product."
        ),
    ),
    DirectPlatform(
        id="anthropic",
        name="Anthropic Claude",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        response_path="/v1/messages",
        models_path="/v1/models",
        auth_modes=("api_key", "oauth"),
        env_names=("ANTHROPIC_API_KEY",),
        default_model="claude-sonnet-4-6",
        stability="stable",
        note="API-key access is stable; Claude subscription OAuth is experimental.",
    ),
    DirectPlatform(
        id="github_copilot",
        name="GitHub Copilot",
        protocol="responses",
        base_url="https://api.individual.githubcopilot.com",
        response_path="/responses",
        models_path="/models",
        auth_modes=("oauth",),
        env_names=(),
        default_model="gpt-5.6-sol",
        stability="experimental",
        subscription=True,
        note=(
            "Uses GitHub device login and Copilot editor endpoints, which are not a "
            "general public inference API."
        ),
    ),
    DirectPlatform(
        id="xai",
        name="xAI",
        protocol="responses",
        base_url="https://api.x.ai/v1",
        response_path="/responses",
        models_path="/models",
        auth_modes=("api_key", "oauth"),
        env_names=("XAI_API_KEY",),
        default_model="grok-4",
        stability="stable",
        note="Responses API with API-key access; subscription OAuth is experimental.",
    ),
    DirectPlatform(
        id="openrouter",
        name="OpenRouter",
        protocol="responses",
        base_url="https://openrouter.ai/api/v1",
        response_path="/responses",
        models_path="/models",
        auth_modes=("api_key", "oauth"),
        env_names=("OPENROUTER_API_KEY",),
        default_model="openrouter/auto",
        stability="beta",
        note="OpenRouter's OpenAI-compatible Responses API is currently beta.",
    ),
    DirectPlatform(
        id="kiro_direct",
        name="Kiro Account (direct)",
        protocol="kiro",
        base_url="https://q.us-east-1.amazonaws.com",
        response_path="/generateAssistantResponse",
        models_path=None,
        auth_modes=("oauth",),
        env_names=(),
        default_model="gpt-5.6-sol",
        stability="experimental",
        subscription=True,
        note=(
            "Direct AWS Builder ID/Identity Center path. Kiro CLI remains the "
            "recommended compatibility fallback."
        ),
    ),
)

DIRECT_PLATFORMS = {platform.id: platform for platform in _PLATFORMS}
DIRECT_PLATFORM_IDS = frozenset(DIRECT_PLATFORMS)


CURATED_MODELS: dict[str, tuple[tuple[str, str], ...]] = {
    "openai_codex": (
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("gpt-5.5", "GPT-5.5"),
    ),
    "kiro_direct": (
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("claude-opus-4.8", "Claude Opus 4.8"),
        ("claude-opus-4.7", "Claude Opus 4.7"),
        ("claude-opus-4.6", "Claude Opus 4.6"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-sonnet-4.6", "Claude Sonnet 4.6"),
        ("claude-sonnet-4.5", "Claude Sonnet 4.5"),
        ("claude-haiku-4.5", "Claude Haiku 4.5"),
        ("deepseek-3.2", "DeepSeek 3.2"),
        ("minimax-m2.5", "MiniMax M2.5"),
        ("glm-5", "GLM-5"),
        ("qwen3-coder-next", "Qwen3 Coder Next"),
        ("auto", "Auto"),
    ),
}


def direct_platform(platform_id: str) -> DirectPlatform:
    try:
        return DIRECT_PLATFORMS[platform_id]
    except KeyError as exc:
        raise ValueError(f"Unknown direct platform: {platform_id}") from exc


def direct_platform_catalog() -> list[dict[str, object]]:
    return [platform.safe_dict() for platform in _PLATFORMS]


def curated_models(platform_id: str) -> list[dict[str, str]]:
    return [
        {"id": model_id, "displayName": display_name}
        for model_id, display_name in CURATED_MODELS.get(platform_id, ())
    ]
