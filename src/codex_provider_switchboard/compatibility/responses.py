from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .profiles import ProviderCapabilities

_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_TRANSPORT_METADATA_KEY = "_switchboard_transport"
_FORWARDED_HEADERS = {
    "openai-beta": "OpenAI-Beta",
    "x-openai-subagent": "x-openai-subagent",
    "x-codex-parent-thread-id": "x-codex-parent-thread-id",
    "x-codex-turn-metadata": "x-codex-turn-metadata",
}
_TOOL_SEARCH_NAME = "tool_search"
_TOOL_SEARCH_DESCRIPTION = (
    "Search and load Codex tools, plugins, connectors, and MCP namespaces for "
    "the current task."
)
_TOOL_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query for tools or connectors to load.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of tool groups to return.",
        },
    },
    "required": ["query"],
}
_CUSTOM_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
}
_CLIENT_TOOL_TYPES = frozenset({"custom", "function", "namespace", "tool_search"})
_TERMINAL_EVENTS = frozenset(
    {
        "response.completed",
        "response.done",
        "response.failed",
        "response.incomplete",
        "response.cancelled",
        "response.canceled",
    }
)
_TOOL_CALL_CONTEXT_TYPES = frozenset(
    {
        "tool_call",
        "function_call",
        "local_shell_call",
        "tool_search_call",
        "custom_tool_call",
        "mcp_tool_call",
    }
)
_TOOL_CALL_OUTPUT_TYPES = frozenset(
    {
        "function_call_output",
        "local_shell_call_output",
        "tool_search_output",
        "custom_tool_call_output",
        "mcp_tool_call_output",
    }
)


class ResponsesCompatibilityError(ValueError):
    """A Responses request cannot be translated without changing its meaning."""


@dataclass(frozen=True, slots=True)
class NamespaceToolName:
    namespace: str
    name: str


@dataclass(frozen=True, slots=True)
class ResponsesToolMapping:
    custom_tools: frozenset[str] = frozenset()
    tool_search: bool = False
    namespace_tools: dict[str, NamespaceToolName] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return (
            not self.custom_tools and not self.tool_search and not self.namespace_tools
        )


@dataclass(frozen=True, slots=True)
class AdaptedResponsesRequest:
    body: dict[str, Any]
    mapping: ResponsesToolMapping = field(default_factory=ResponsesToolMapping)


@dataclass(frozen=True, slots=True)
class ToolContinuationCoverage:
    has_outputs: bool
    missing_call_id: bool
    context_covers_all_call_ids: bool


def analyze_tool_continuation_coverage(
    body: dict[str, Any],
) -> ToolContinuationCoverage:
    input_value = body.get("input")
    if not isinstance(input_value, list):
        return ToolContinuationCoverage(False, False, True)
    outputs: set[str] = set()
    contexts: set[str] = set()
    missing_call_id = False
    has_outputs = False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in _TOOL_CALL_OUTPUT_TYPES:
            has_outputs = True
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                missing_call_id = True
            else:
                outputs.add(call_id.strip())
        elif item_type in _TOOL_CALL_CONTEXT_TYPES:
            call_id = item.get("call_id") or item.get("id")
            if isinstance(call_id, str) and call_id.strip():
                contexts.add(call_id.strip())
        elif item_type == "item_reference":
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id.strip():
                contexts.add(item_id.strip())
    return ToolContinuationCoverage(
        has_outputs=has_outputs,
        missing_call_id=missing_call_id,
        context_covers_all_call_ids=(
            not missing_call_id and outputs.issubset(contexts)
        ),
    )


def _valid_header_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 16_384 or any(char in value for char in "\r\n"):
        return None
    return value


def bind_transport_context(
    body: dict[str, Any], headers: Mapping[str, str]
) -> dict[str, Any]:
    """Attach an allow-listed HTTP/WS context without mixing it into model input."""

    value = dict(body)
    existing = body.get("client_metadata")
    metadata = dict(existing) if isinstance(existing, dict) else {}
    metadata.pop(_TRANSPORT_METADATA_KEY, None)
    forwarded: dict[str, str] = {}
    for source, destination in _FORWARDED_HEADERS.items():
        header = _valid_header_value(headers.get(source))
        if header is not None:
            forwarded[destination] = header
    if forwarded:
        metadata[_TRANSPORT_METADATA_KEY] = forwarded
    if metadata or "client_metadata" in body:
        value["client_metadata"] = metadata
    return value


def forwarded_codex_headers(body: dict[str, Any]) -> dict[str, str]:
    metadata = body.get("client_metadata")
    if not isinstance(metadata, dict):
        return {}
    transport = metadata.get(_TRANSPORT_METADATA_KEY)
    if not isinstance(transport, dict):
        return {}
    allowed = set(_FORWARDED_HEADERS.values())
    result: dict[str, str] = {}
    for name, raw in transport.items():
        value = _valid_header_value(raw)
        if name in allowed and value is not None:
            result[name] = value
    return result


def _canonical_tool(tool: dict[str, Any]) -> str:
    public = {key: value for key, value in tool.items() if not key.startswith("_")}
    try:
        return json.dumps(
            public,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResponsesCompatibilityError(
            "Tool declaration contains a value that cannot be encoded as JSON."
        ) from exc


def _tool_search_declaration(candidate: dict[str, Any]) -> dict[str, Any]:
    description = candidate.get("description")
    parameters = candidate.get("parameters")
    return {
        "type": "tool_search",
        "name": _TOOL_SEARCH_NAME,
        "description": (
            description if isinstance(description, str) else _TOOL_SEARCH_DESCRIPTION
        ),
        "parameters": (
            copy.deepcopy(parameters)
            if isinstance(parameters, dict)
            else copy.deepcopy(_TOOL_SEARCH_SCHEMA)
        ),
    }


def _discovered_tools(item: dict[str, Any]) -> list[Any]:
    if item.get("status") not in {None, "completed"}:
        return []
    tools = item.get("tools")
    if isinstance(tools, list):
        return tools
    output = item.get("output")
    if isinstance(output, list):
        return output
    if isinstance(output, dict) and isinstance(output.get("tools"), list):
        return output["tools"]
    if isinstance(output, str) and len(output) <= 2 * 1_048_576:
        try:
            decoded = json.loads(output)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return []
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict) and isinstance(decoded.get("tools"), list):
            return decoded["tools"]
    return []


def collect_request_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect static, additional, and dynamically discovered Codex tools."""

    collected: list[dict[str, Any]] = []
    seen: dict[str, str] = {}

    def add(candidate: Any, namespace: str | None = None) -> None:
        if not isinstance(candidate, dict):
            return
        raw_type = candidate.get("type")
        if raw_type == "namespace":
            namespace_name = candidate.get("name")
            if not isinstance(namespace_name, str) or not _SAFE_TOOL_NAME.fullmatch(
                namespace_name
            ):
                return
            children = candidate.get("tools")
            if not isinstance(children, list):
                children = candidate.get("children")
            if isinstance(children, list):
                for child in children:
                    add(child, namespace_name)
            return
        if raw_type == "tool_search":
            if namespace is not None:
                return
            normalized = _tool_search_declaration(candidate)
            public_name = _TOOL_SEARCH_NAME
        else:
            wire_name = candidate.get("name")
            if raw_type is None and isinstance(wire_name, str):
                raw_type = "custom"
            if raw_type not in {"function", "custom"} or not isinstance(wire_name, str):
                return
            if not _SAFE_TOOL_NAME.fullmatch(wire_name):
                return
            public_name = f"{namespace}.{wire_name}" if namespace else wire_name
            if not _SAFE_TOOL_NAME.fullmatch(public_name):
                return
            normalized = copy.deepcopy(candidate)
            normalized["name"] = public_name
            normalized["type"] = raw_type
            if namespace:
                normalized["_namespace"] = namespace
                normalized["_wire_name"] = wire_name

        canonical = _canonical_tool(normalized)
        previous = seen.get(public_name)
        if previous is not None:
            if previous != canonical:
                raise ResponsesCompatibilityError(
                    f"Conflicting tool declarations for '{public_name}'."
                )
            return
        seen[public_name] = canonical
        collected.append(normalized)

    tools = body.get("tools")
    if isinstance(tools, list):
        for candidate in tools:
            add(candidate)

    input_value = body.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "additional_tools":
                for candidate_list in item.values():
                    if isinstance(candidate_list, list):
                        for candidate in candidate_list:
                            add(candidate)
            elif item_type in {"tool_search_call", "tool_search_output"}:
                if _TOOL_SEARCH_NAME not in seen:
                    add({"type": "tool_search"})
                if item_type == "tool_search_output":
                    for candidate in _discovered_tools(item):
                        add(candidate)
            elif item_type in {"function_call", "custom_tool_call"}:
                name = item.get("name")
                namespace = item.get("namespace")
                if not isinstance(name, str):
                    continue
                public_name = (
                    f"{namespace}.{name}"
                    if isinstance(namespace, str)
                    and not name.startswith(f"{namespace}.")
                    else name
                )
                if public_name in seen:
                    continue
                add(
                    {
                        "type": (
                            "custom" if item_type == "custom_tool_call" else "function"
                        ),
                        "name": name,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                    namespace if isinstance(namespace, str) else None,
                )

    if not collected:
        instructions = body.get("instructions")
        encoded = (
            instructions
            if isinstance(instructions, str)
            else json.dumps(instructions, ensure_ascii=False)
        )
        if "FREEFORM" in encoded and re.search(r"\bexec\b", encoded):
            add({"name": "exec", "type": "custom"})
    return collected


def promote_additional_tools(body: dict[str, Any]) -> dict[str, Any]:
    """Move Codex ``additional_tools`` declarations to native ``tools``."""

    payload = copy.deepcopy(body)
    existing = payload.get("tools")
    tools: list[Any] = []
    identities: dict[tuple[str, str], str] = {}

    def append(candidate: Any, *, from_additional: bool = False) -> None:
        if not isinstance(candidate, dict):
            if not from_additional:
                tools.append(copy.deepcopy(candidate))
            return

        normalized = copy.deepcopy(candidate)
        tool_type = normalized.get("type")
        name = normalized.get("name")
        if from_additional and tool_type is None and isinstance(name, str):
            # Codex may omit ``type`` for additional free-form client tools.
            tool_type = "custom"
            normalized["type"] = tool_type

        if tool_type not in _CLIENT_TOOL_TYPES:
            if not from_additional:
                # Native built-ins such as web_search must pass through untouched.
                tools.append(normalized)
            return
        if tool_type == "tool_search":
            identity_name = name if isinstance(name, str) else ""
        elif isinstance(name, str):
            identity_name = name
        else:
            if not from_additional:
                tools.append(normalized)
            return

        key = (str(tool_type), identity_name)
        canonical = _canonical_tool(normalized)
        previous = identities.get(key)
        if previous is not None:
            if previous != canonical:
                raise ResponsesCompatibilityError(
                    f"Conflicting tool declarations for '{identity_name or tool_type}'."
                )
            return
        identities[key] = canonical
        tools.append(normalized)

    for candidate in existing if isinstance(existing, list) else []:
        append(candidate)

    input_value = payload.get("input")
    if isinstance(input_value, list):
        retained: list[Any] = []
        for item in input_value:
            if not (isinstance(item, dict) and item.get("type") == "additional_tools"):
                retained.append(item)
                continue
            for candidate_list in item.values():
                if isinstance(candidate_list, list):
                    for candidate in candidate_list:
                        append(candidate, from_additional=True)
        payload["input"] = retained
    if tools or isinstance(existing, list):
        payload["tools"] = tools
    return payload


def prepare_compaction_request(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Build the narrower official ``/responses/compact`` request schema."""

    multi_agent = body.get("multi_agent")
    if isinstance(multi_agent, dict) and multi_agent.get("enabled") is True:
        raise ResponsesCompatibilityError(
            "Explicit /responses/compact is not available in multi-agent mode; "
            "the native service compacts each agent automatically."
        )
    payload: dict[str, Any] = {"model": model}
    if "input" in body:
        input_value = body.get("input")
        if isinstance(input_value, list):
            compact_input: Any = []
            for item in input_value:
                if isinstance(item, dict) and item.get("type") == "additional_tools":
                    continue
                copied = copy.deepcopy(item)
                if (
                    isinstance(copied, dict)
                    and copied.get("type") in _TOOL_CALL_CONTEXT_TYPES
                ):
                    copied.pop("namespace", None)
                compact_input.append(copied)
        else:
            compact_input = copy.deepcopy(input_value)
        payload["input"] = compact_input

    for key in (
        "instructions",
        "previous_response_id",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "service_tier",
    ):
        if key in body:
            payload[key] = copy.deepcopy(body[key])
    return payload


def _flattened_name(namespace: str, name: str) -> str:
    full = f"{namespace}__{name}"
    if len(full) <= 64:
        return full
    suffix = f"__{hashlib.sha256(full.encode('utf-8')).hexdigest()[:8]}"
    return f"{full[: 64 - len(suffix)]}{suffix}"


def _upstream_tool(
    tool: dict[str, Any],
    *,
    occupied: set[str],
    custom_names: set[str],
    namespace_names: dict[str, NamespaceToolName],
) -> dict[str, Any]:
    tool_type = str(tool.get("type") or "")
    public_name = str(tool.get("name") or "")
    namespace = tool.get("_namespace")
    wire_name = str(tool.get("_wire_name") or public_name)
    upstream_name = (
        _flattened_name(namespace, wire_name)
        if isinstance(namespace, str)
        else public_name
    )
    if upstream_name in occupied:
        raise ResponsesCompatibilityError(
            f"Tool '{public_name}' conflicts after compatibility name lowering."
        )
    occupied.add(upstream_name)
    if isinstance(namespace, str):
        namespace_names[upstream_name] = NamespaceToolName(namespace, wire_name)

    if tool_type == "tool_search":
        return {
            "type": "function",
            "name": _TOOL_SEARCH_NAME,
            "description": str(tool.get("description") or _TOOL_SEARCH_DESCRIPTION),
            "parameters": copy.deepcopy(
                tool.get("parameters")
                if isinstance(tool.get("parameters"), dict)
                else _TOOL_SEARCH_SCHEMA
            ),
        }

    result = {
        key: copy.deepcopy(value)
        for key, value in tool.items()
        if not key.startswith("_") and key != "defer_loading"
    }
    result["name"] = upstream_name
    if tool_type == "custom":
        result["type"] = "function"
        result["parameters"] = copy.deepcopy(_CUSTOM_TOOL_SCHEMA)
        result.pop("format", None)
        custom_names.add(upstream_name)
    return result


def _drop_lowered_item_id(item: dict[str, Any]) -> None:
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id and not item_id.startswith("fc"):
        item.pop("id", None)


def _json_string(value: Any, *, fallback: str = "{}") -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        return fallback


def _output_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return _json_string(value, fallback="")


def _rewrite_history(value: Any, mapping: ResponsesToolMapping) -> None:
    if isinstance(value, list):
        for item in value:
            _rewrite_history(item, mapping)
        return
    if not isinstance(value, dict):
        return

    item_type = value.get("type")
    name = value.get("name")
    if item_type == "custom_tool_call" and isinstance(name, str):
        upstream_name = name
        namespace = value.get("namespace")
        if isinstance(namespace, str):
            upstream_name = _flattened_name(namespace, name)
        if upstream_name in mapping.custom_tools:
            value["type"] = "function_call"
            value["name"] = upstream_name
            value["arguments"] = _json_string({"input": value.get("input", "")})
            value.pop("input", None)
            value.pop("namespace", None)
            _drop_lowered_item_id(value)
    elif item_type == "custom_tool_call_output":
        value["type"] = "function_call_output"
        if "output" in value:
            value["output"] = _output_string(value.get("output"))
        _drop_lowered_item_id(value)
    elif item_type == "tool_search_call" and mapping.tool_search:
        value["type"] = "function_call"
        value["name"] = _TOOL_SEARCH_NAME
        value["arguments"] = _json_string(value.get("arguments"))
        value.pop("execution", None)
        value.pop("namespace", None)
        _drop_lowered_item_id(value)
    elif item_type == "tool_search_output" and mapping.tool_search:
        call_id = value.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ResponsesCompatibilityError(
                "tool_search_output requires a non-empty call_id."
            )
        output = value.get("output") if "output" in value else value.get("tools")
        if "output" not in value and "tools" not in value:
            raise ResponsesCompatibilityError(
                "tool_search_output requires output or tools."
            )
        value["type"] = "function_call_output"
        value["output"] = _output_string(output)
        for key in ("tools", "status", "execution"):
            value.pop(key, None)
        _drop_lowered_item_id(value)
    elif item_type == "function_call" and isinstance(name, str):
        namespace = value.get("namespace")
        if isinstance(namespace, str):
            flattened = _flattened_name(namespace, name)
            identity = mapping.namespace_tools.get(flattened)
            if identity == NamespaceToolName(namespace, name):
                value["name"] = flattened
                value.pop("namespace", None)

    for child in tuple(value.values()):
        _rewrite_history(child, mapping)


def _rewrite_tool_choice(body: dict[str, Any], mapping: ResponsesToolMapping) -> None:
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return
    choice_type = choice.get("type")
    name = choice.get("name")
    namespace = choice.get("namespace")
    if choice_type == "tool_search" and mapping.tool_search:
        body["tool_choice"] = {"type": "function", "name": _TOOL_SEARCH_NAME}
    elif choice_type == "custom" and isinstance(name, str):
        upstream_name = (
            _flattened_name(namespace, name) if isinstance(namespace, str) else name
        )
        if upstream_name in mapping.custom_tools:
            body["tool_choice"] = {"type": "function", "name": upstream_name}
    elif isinstance(name, str) and isinstance(namespace, str):
        flattened = _flattened_name(namespace, name)
        if flattened in mapping.namespace_tools:
            body["tool_choice"] = {"type": "function", "name": flattened}


def adapt_responses_request(
    body: dict[str, Any], capabilities: ProviderCapabilities
) -> AdaptedResponsesRequest:
    """Lower Codex client tools only when the selected upstream requires it."""

    payload = copy.deepcopy(body)
    if not capabilities.requires_function_lowering:
        return AdaptedResponsesRequest(payload)

    collected = collect_request_tools(payload)
    occupied: set[str] = set()
    custom_names: set[str] = set()
    namespace_names: dict[str, NamespaceToolName] = {}
    lowered: list[Any] = []

    original_tools = payload.get("tools")
    if isinstance(original_tools, list):
        for tool in original_tools:
            if isinstance(tool, dict) and tool.get("type") in _CLIENT_TOOL_TYPES:
                continue
            lowered.append(copy.deepcopy(tool))

    tool_search = False
    for tool in collected:
        if tool.get("type") == "tool_search":
            tool_search = True
        lowered.append(
            _upstream_tool(
                tool,
                occupied=occupied,
                custom_names=custom_names,
                namespace_names=namespace_names,
            )
        )

    mapping = ResponsesToolMapping(
        custom_tools=frozenset(custom_names),
        tool_search=tool_search,
        namespace_tools=namespace_names,
    )
    if lowered or isinstance(original_tools, list):
        payload["tools"] = lowered

    input_value = payload.get("input")
    if isinstance(input_value, list):
        payload["input"] = [
            item
            for item in input_value
            if not (isinstance(item, dict) and item.get("type") == "additional_tools")
        ]
        _rewrite_history(payload["input"], mapping)
    _rewrite_tool_choice(payload, mapping)

    # These fields describe OpenAI-hosted orchestration. Function-only upstreams
    # can still call lowered collaboration tools, but cannot host this protocol.
    payload.pop("multi_agent", None)
    payload.pop("context_management", None)
    return AdaptedResponsesRequest(payload, mapping)


def _extract_custom_input(arguments: Any) -> str:
    raw = _json_string(arguments)
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return raw
    if isinstance(decoded, dict) and isinstance(decoded.get("input"), str):
        return decoded["input"]
    return raw


def _tool_search_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return copy.deepcopy(arguments)
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _restore_function_call(item: dict[str, Any], mapping: ResponsesToolMapping) -> None:
    if item.get("type") != "function_call":
        return
    name = item.get("name")
    if not isinstance(name, str):
        return
    if name in mapping.custom_tools:
        item["type"] = "custom_tool_call"
        item["input"] = _extract_custom_input(item.get("arguments"))
        item.pop("arguments", None)
        item.pop("namespace", None)
        return
    if mapping.tool_search and name == _TOOL_SEARCH_NAME:
        item["type"] = "tool_search_call"
        item["arguments"] = _tool_search_arguments(item.get("arguments"))
        item["execution"] = "client"
        item.pop("name", None)
        item.pop("namespace", None)
        return
    identity = mapping.namespace_tools.get(name)
    if identity is not None:
        item["name"] = identity.name
        item["namespace"] = identity.namespace


def restore_response_value(value: Any, mapping: ResponsesToolMapping) -> Any:
    restored = copy.deepcopy(value)

    def visit(candidate: Any) -> None:
        if isinstance(candidate, list):
            for item in candidate:
                visit(item)
        elif isinstance(candidate, dict):
            _restore_function_call(candidate, mapping)
            for child in tuple(candidate.values()):
                visit(child)

    visit(restored)
    return restored


@dataclass(slots=True)
class _StreamCall:
    kind: str
    name: str
    call_id: str
    item_id: str
    output_index: int
    arguments: str = ""


class ResponsesStreamRestorer:
    """Restore a lowered function lifecycle to the Codex Responses lifecycle."""

    def __init__(self, mapping: ResponsesToolMapping) -> None:
        self.mapping = mapping
        self._calls: dict[str, _StreamCall] = {}
        self._by_output: dict[int, _StreamCall] = {}
        self._next_sequence: int | None = None

    def _record(self, event: dict[str, Any]) -> _StreamCall | None:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return None
        name = item.get("name")
        if not isinstance(name, str):
            return None
        if name in self.mapping.custom_tools:
            kind = "custom"
        elif self.mapping.tool_search and name == _TOOL_SEARCH_NAME:
            kind = "tool_search"
        else:
            return None
        item_id = str(item.get("id") or "")
        call_id = str(item.get("call_id") or "")
        output_index = int(event.get("output_index") or 0)
        call = self._calls.get(item_id) or self._calls.get(call_id)
        if call is None:
            call = _StreamCall(kind, name, call_id, item_id, output_index)
            if item_id:
                self._calls[item_id] = call
            if call_id:
                self._calls[call_id] = call
            self._by_output[output_index] = call
        arguments = item.get("arguments")
        if isinstance(arguments, str) and arguments:
            call.arguments = arguments
        return call

    def _lookup(self, event: dict[str, Any]) -> _StreamCall | None:
        for key in (event.get("item_id"), event.get("call_id")):
            if isinstance(key, str) and key in self._calls:
                return self._calls[key]
        output_index = event.get("output_index")
        if isinstance(output_index, int):
            return self._by_output.get(output_index)
        return None

    def _forget(self, call: _StreamCall) -> None:
        for key in (call.item_id, call.call_id):
            if key:
                self._calls.pop(key, None)
        self._by_output.pop(call.output_index, None)

    def _restore_namespace_event(self, event: dict[str, Any]) -> None:
        item = event.get("item")
        if isinstance(item, dict):
            _restore_function_call(item, self.mapping)
        if event.get("type") in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            name = event.get("name")
            identity = (
                self.mapping.namespace_tools.get(name)
                if isinstance(name, str)
                else None
            )
            if identity is not None:
                event["name"] = identity.name

    def restore(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        event = copy.deepcopy(event)
        sequence = event.get("sequence_number")
        if self._next_sequence is None and isinstance(sequence, int):
            self._next_sequence = sequence
        emitted: list[dict[str, Any]] = []

        def emit(value: dict[str, Any]) -> None:
            if self._next_sequence is not None:
                value["sequence_number"] = self._next_sequence
                self._next_sequence += 1
            emitted.append(value)

        event_type = event.get("type")
        if event_type == "response.output_item.added":
            call = self._record(event)
            item = event.get("item")
            if call is not None and isinstance(item, dict):
                if call.kind == "custom":
                    item["type"] = "custom_tool_call"
                    item["input"] = ""
                    item.pop("arguments", None)
                    item.pop("namespace", None)
                else:
                    item["type"] = "tool_search_call"
                    item["arguments"] = {}
                    item["execution"] = "client"
                    item.pop("name", None)
                    item.pop("namespace", None)
            else:
                self._restore_namespace_event(event)
            emit(event)
            return emitted

        if event_type == "response.function_call_arguments.delta":
            call = self._lookup(event)
            if call is not None:
                delta = event.get("delta")
                if isinstance(delta, str):
                    call.arguments += delta
                return emitted
            self._restore_namespace_event(event)
            emit(event)
            return emitted

        if event_type == "response.function_call_arguments.done":
            call = self._lookup(event)
            if call is not None:
                arguments = event.get("arguments")
                if isinstance(arguments, str) and arguments:
                    call.arguments = arguments
                if call.kind == "custom":
                    custom_input = _extract_custom_input(call.arguments)
                    if custom_input:
                        emit(
                            {
                                "type": "response.custom_tool_call_input.delta",
                                "output_index": call.output_index,
                                "item_id": call.item_id,
                                "delta": custom_input,
                            }
                        )
                    emit(
                        {
                            "type": "response.custom_tool_call_input.done",
                            "output_index": call.output_index,
                            "item_id": call.item_id,
                            "call_id": call.call_id,
                            "name": call.name,
                            "input": custom_input,
                        }
                    )
                return emitted
            self._restore_namespace_event(event)
            emit(event)
            return emitted

        if event_type == "response.output_item.done":
            call = self._record(event)
            item = event.get("item")
            if call is not None and isinstance(item, dict):
                if call.kind == "custom":
                    item["type"] = "custom_tool_call"
                    item["input"] = _extract_custom_input(call.arguments)
                    item.pop("arguments", None)
                    item.pop("namespace", None)
                else:
                    item["type"] = "tool_search_call"
                    item["arguments"] = _tool_search_arguments(call.arguments)
                    item["execution"] = "client"
                    item.pop("name", None)
                    item.pop("namespace", None)
                self._forget(call)
            else:
                self._restore_namespace_event(event)
            emit(event)
            return emitted

        if event_type in _TERMINAL_EVENTS:
            event = restore_response_value(event, self.mapping)
        else:
            self._restore_namespace_event(event)
        emit(event)
        return emitted
