from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CompatibilityProfileId = Literal[
    "native_codex",
    "function_only",
    "prompt_bridge",
]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Responses features an upstream can consume without translation."""

    id: CompatibilityProfileId
    native_custom_tools: bool
    native_tool_search: bool
    native_namespaces: bool
    native_multi_agent: bool
    native_compaction: bool
    forward_codex_headers: bool

    @property
    def requires_function_lowering(self) -> bool:
        return self.id == "function_only"

    @property
    def uses_prompt_bridge(self) -> bool:
        return self.id == "prompt_bridge"


NATIVE_CODEX = ProviderCapabilities(
    id="native_codex",
    native_custom_tools=True,
    native_tool_search=True,
    native_namespaces=True,
    native_multi_agent=True,
    native_compaction=True,
    forward_codex_headers=True,
)

FUNCTION_ONLY = ProviderCapabilities(
    id="function_only",
    native_custom_tools=False,
    native_tool_search=False,
    native_namespaces=False,
    native_multi_agent=False,
    native_compaction=False,
    forward_codex_headers=False,
)

PROMPT_BRIDGE = ProviderCapabilities(
    id="prompt_bridge",
    native_custom_tools=False,
    native_tool_search=False,
    native_namespaces=False,
    native_multi_agent=False,
    native_compaction=False,
    forward_codex_headers=False,
)

_PROFILES: dict[str, ProviderCapabilities] = {
    profile.id: profile
    for profile in (
        NATIVE_CODEX,
        FUNCTION_ONLY,
        PROMPT_BRIDGE,
    )
}


def compatibility_profile(value: str) -> ProviderCapabilities:
    try:
        return _PROFILES[value]
    except KeyError as exc:
        raise ValueError(f"Unknown Responses compatibility profile: {value}") from exc
