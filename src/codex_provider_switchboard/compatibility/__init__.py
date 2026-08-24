"""Pure protocol adapters between Codex Responses and provider wire formats."""

from .profiles import (
    FUNCTION_ONLY,
    NATIVE_CODEX,
    PROMPT_BRIDGE,
    ProviderCapabilities,
    compatibility_profile,
)
from .responses import (
    AdaptedResponsesRequest,
    ResponsesCompatibilityError,
    ResponsesStreamRestorer,
    ResponsesToolMapping,
    ToolContinuationCoverage,
    adapt_responses_request,
    analyze_tool_continuation_coverage,
    bind_transport_context,
    collect_request_tools,
    forwarded_codex_headers,
    prepare_compaction_request,
    promote_additional_tools,
)

__all__ = [
    "FUNCTION_ONLY",
    "NATIVE_CODEX",
    "PROMPT_BRIDGE",
    "AdaptedResponsesRequest",
    "ProviderCapabilities",
    "ResponsesCompatibilityError",
    "ResponsesStreamRestorer",
    "ResponsesToolMapping",
    "ToolContinuationCoverage",
    "adapt_responses_request",
    "analyze_tool_continuation_coverage",
    "bind_transport_context",
    "collect_request_tools",
    "compatibility_profile",
    "forwarded_codex_headers",
    "prepare_compaction_request",
    "promote_additional_tools",
]
