from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CREDIT_RE = re.compile(r"^\s*[▸>]?[ ]*Credits:\s*", re.IGNORECASE)
_CONTEXT_OVERFLOW_PREFIX_RE = re.compile(
    r"^\s*>?\s*The context window has overflowed,\s*"
    r"summarizing the history",
    re.IGNORECASE,
)
_TRUNCATED_OUTPUT_RE = re.compile(
    r"^\s*>?\s*(?:CODEX_SWITCHBOARD(?:_[A-Z0-9]*)?"
    r"(?:\.\.\.|\u2026)\s*)?content truncated due to length[.!]?\s*$",
    re.IGNORECASE,
)
_CONTEXT_STATUS_BASE = "the context window has overflowed, summarizing the history"
_TRUNCATION_STATUS_BASE = "content truncated due to length"
_PARTIAL_BRIDGE_STATUS_BASE = "codex_switchboard_"
_STREAM_MESSAGE_PREFIX_RE = re.compile(
    r'\s*\{\s*"kind"\s*:\s*"message"\s*,\s*"text"\s*:\s*"',
    re.DOTALL,
)
_STREAM_COMMENTARY_PREFIX_RE = re.compile(
    r'\s*\{\s*"kind"\s*:\s*"tool_calls"\s*,\s*'
    r'"commentary"\s*:\s*"',
    re.DOTALL,
)
_BRIDGE_PREFIX = "CODEX_SWITCHBOARD_BRIDGE"
_ERROR_PREFIX = "[provider-switchboard]"
_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_MAX_TOOL_CALLS = 64
_MAX_TOOL_PAYLOAD_CHARS = 4 * 1_048_576
_MAX_COMMENTARY_CHARS = 16_384

_KIRO_EFFORT_MAP = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra_high": "xhigh",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}


@dataclass(frozen=True)
class BridgeToolCall:
    name: str
    tool_type: str
    payload: str
    namespace: str | None = None


@dataclass(frozen=True)
class BridgeResult:
    text: str | None
    tool_calls: tuple[BridgeToolCall, ...] = ()
    commentary: str | None = None


class BridgeProtocolError(ValueError):
    """Raised when model output contains leaked or malformed bridge control data."""


class BridgeUpstreamRetryableError(BridgeProtocolError):
    """Raised for a bounded upstream status that is safe to retry once."""

    reason = "upstream_status"


class BridgeUpstreamContextOverflowError(BridgeUpstreamRetryableError):
    """Raised when an upstream CLI returns its context status instead of an answer."""

    reason = "context_overflow"


class BridgeUpstreamOutputTruncatedError(BridgeUpstreamRetryableError):
    """Raised when an upstream CLI truncates bridge protocol output."""

    reason = "output_truncated"


class BridgePromptTooLargeError(ValueError):
    """Raised when a bridge prompt cannot fit without truncating the active turn."""

    def __init__(self, rendered_bytes: int, limit: int) -> None:
        super().__init__(
            "Rendered bridge prompt remains too large after compacting transport "
            "metadata and trimming the oldest complete history turns "
            f"({rendered_bytes} bytes; limit {limit}). Start a new task or reduce "
            "the current instructions, tool catalog, or message."
        )
        self.rendered_bytes = rendered_bytes
        self.limit = limit


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_json_constant)


def new_nonce() -> str:
    return secrets.token_hex(8)


def clean_kiro_stdout(value: str) -> str:
    """Remove terminal control codes and Kiro's display prefix from stdout."""
    value = _OSC_RE.sub("", value)
    value = _CSI_RE.sub("", value).replace("\r", "")
    lines = value.splitlines()

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if lines:
        lines[0] = re.sub(r"^\s*>\s?", "", lines[0], count=1)

    lines = [line for line in lines if not _CREDIT_RE.match(line)]
    return "\n".join(lines).strip()


def _classify_upstream_status(value: str) -> str | None:
    """Classify only the short control statuses emitted by the upstream CLI."""
    match = _CONTEXT_OVERFLOW_PREFIX_RE.match(value)
    if match is not None:
        tail = value[match.end() :].strip()
        if tail in {"", "...", "\u2026"}:
            return "context_overflow"
        if tail.startswith("..."):
            tail = tail[3:].strip()
        elif tail.startswith("\u2026"):
            tail = tail[1:].strip()
        if _TRUNCATED_OUTPUT_RE.fullmatch(tail):
            return "context_overflow"
    if _TRUNCATED_OUTPUT_RE.fullmatch(value):
        return "output_truncated"
    return None


def _raise_for_upstream_status(value: str) -> None:
    status = _classify_upstream_status(value)
    if status == "context_overflow":
        raise BridgeUpstreamContextOverflowError(
            "The upstream CLI reported a context-window overflow instead of "
            "returning a bridge response."
        )
    if status == "output_truncated":
        raise BridgeUpstreamOutputTruncatedError(
            "The upstream CLI truncated bridge protocol output instead of "
            "returning a complete response."
        )


def _could_be_upstream_status(value: str) -> bool:
    """Keep a streamed status private until enough text disproves it."""
    normalized = value.lstrip().casefold()
    if not normalized:
        return True
    if _CONTEXT_STATUS_BASE.startswith(normalized):
        return True
    if normalized.startswith(_CONTEXT_STATUS_BASE):
        tail = normalized[len(_CONTEXT_STATUS_BASE) :].lstrip()
        if tail in {"", ".", "..", "...", "\u2026"}:
            return True
        if tail.startswith("..."):
            tail = tail[3:].lstrip()
        elif tail.startswith("\u2026"):
            tail = tail[1:].lstrip()
        if tail.startswith(">"):
            tail = tail[1:].lstrip()
        return (
            _TRUNCATION_STATUS_BASE.startswith(tail)
            or _PARTIAL_BRIDGE_STATUS_BASE.startswith(tail)
            or tail.startswith(_PARTIAL_BRIDGE_STATUS_BASE)
        ) and len(normalized) <= 1_024
    if _TRUNCATION_STATUS_BASE.startswith(normalized):
        return True
    return (
        _PARTIAL_BRIDGE_STATUS_BASE.startswith(normalized)
        or normalized.startswith(_PARTIAL_BRIDGE_STATUS_BASE)
    ) and len(normalized) <= 1_024


def kiro_effort_from_body(body: dict[str, Any]) -> str | None:
    """Map a Responses reasoning effort to the closest Kiro CLI effort."""
    reasoning = body.get("reasoning")
    requested: Any = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if not isinstance(requested, str):
        requested = body.get("reasoning_effort")
    if not isinstance(requested, str):
        return None
    normalized = requested.strip().lower().replace("-", "_")
    return _KIRO_EFFORT_MAP.get(normalized)


def codex_thread_key(body: dict[str, Any]) -> str | None:
    """Return Codex's per-agent thread key when the request provides one.

    Newer Codex builds can share ``prompt_cache_key`` across one live agent tree,
    while ``client_metadata.thread_id`` remains unique to each agent. Prefer the
    latter so a parent and its subagents never resume the same Kiro session.
    """
    client_metadata = body.get("client_metadata")
    if isinstance(client_metadata, dict):
        value = client_metadata.get("thread_id")
        if isinstance(value, str) and value.strip():
            return value
    value = body.get("prompt_cache_key")
    if isinstance(value, str) and value.strip():
        return value
    return None


def codex_thread_key_hash(body: dict[str, Any]) -> str | None:
    """Return a log/path-safe digest without exposing the Codex thread ID."""
    value = codex_thread_key(body)
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _TerminalTextFilter:
    """Incrementally remove CSI/OSC terminal controls from streamed text."""

    def __init__(self) -> None:
        self._state = "text"

    def feed(self, value: str) -> str:
        output: list[str] = []
        for char in value:
            if self._state == "text":
                if char == "\x1b":
                    self._state = "escape"
                elif char != "\r":
                    output.append(char)
            elif self._state == "escape":
                if char == "[":
                    self._state = "csi"
                elif char == "]":
                    self._state = "osc"
                else:
                    self._state = "text"
            elif self._state == "csi":
                if "@" <= char <= "~":
                    self._state = "text"
            elif self._state == "osc":
                if char == "\x07":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_escape"
            elif self._state == "osc_escape":
                if char == "\\":
                    self._state = "text"
                elif char != "\x1b":
                    self._state = "osc"
        return "".join(output)


class StreamingMessageParser:
    """Decode safe user-visible JSON text while an upstream CLI generates it.

    Final answers use ``kind=message``/``text``. Tool rounds may put a short
    ``commentary`` string before the calls array. Only that JSON string is
    released incrementally; tool payloads remain buffered until the complete
    nonce-bound envelope passes strict parsing.
    """

    _ESCAPES: ClassVar[dict[str, str]] = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def __init__(self, nonce: str) -> None:
        self._begin = f"{_BRIDGE_PREFIX}_BEGIN_{nonce}"
        self._filter = _TerminalTextFilter()
        self._buffer = ""
        self._cursor = 0
        self._parts: list[str] = []
        self._visible_pending = ""
        self._escape = False
        self._unicode_digits: list[str] | None = None
        self._pending_high_surrogate: int | None = None
        self.started = False
        self.done = False
        self.error: str | None = None
        self.protocol_contaminated = False
        self.phase: str | None = None

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def _append_codepoint(self, codepoint: int, output: list[str]) -> None:
        pending = self._pending_high_surrogate
        if pending is not None:
            if 0xDC00 <= codepoint <= 0xDFFF:
                combined = 0x10000 + ((pending - 0xD800) << 10) + (codepoint - 0xDC00)
                output.append(chr(combined))
                self._pending_high_surrogate = None
                return
            output.append("\ufffd")
            self._pending_high_surrogate = None

        if 0xD800 <= codepoint <= 0xDBFF:
            self._pending_high_surrogate = codepoint
        elif 0xDC00 <= codepoint <= 0xDFFF:
            output.append("\ufffd")
        else:
            output.append(chr(codepoint))

    def _visible_delta(self, value: str, *, final: bool) -> str:
        """Release text after ruling out control status and marker prefixes."""
        if self.protocol_contaminated:
            return ""
        self._visible_pending += value
        if _BRIDGE_PREFIX in self._visible_pending:
            self.protocol_contaminated = True
            self._visible_pending = ""
            return ""
        if final and _classify_upstream_status(self._visible_pending) is not None:
            self._visible_pending = ""
            return ""
        if not final and _could_be_upstream_status(self._visible_pending):
            return ""
        if final:
            delta = self._visible_pending
            self._visible_pending = ""
            return delta
        retained = 0
        possible_prefix = min(len(self._visible_pending), len(_BRIDGE_PREFIX) - 1)
        for size in range(possible_prefix, 0, -1):
            if self._visible_pending.endswith(_BRIDGE_PREFIX[:size]):
                retained = size
                break
        split_at = len(self._visible_pending) - retained
        delta = self._visible_pending[:split_at]
        self._visible_pending = self._visible_pending[split_at:]
        return delta

    def feed(self, value: str) -> str:
        if self.done or self.error is not None:
            return ""

        self._buffer += self._filter.feed(value)
        if not self.started:
            marker_at = self._buffer.find(self._begin)
            if marker_at < 0:
                return ""
            prefix_at = marker_at + len(self._begin)
            match = _STREAM_MESSAGE_PREFIX_RE.match(self._buffer, prefix_at)
            if match is not None:
                self.phase = "final_answer"
            else:
                match = _STREAM_COMMENTARY_PREFIX_RE.match(self._buffer, prefix_at)
                if match is not None:
                    self.phase = "commentary"
            if match is None:
                return ""
            self.started = True
            self._cursor = match.end()

        output: list[str] = []
        while self._cursor < len(self._buffer) and not self.done:
            char = self._buffer[self._cursor]
            self._cursor += 1

            if self._unicode_digits is not None:
                if char not in "0123456789abcdefABCDEF":
                    self.error = "Invalid Unicode escape in streamed message envelope."
                    break
                self._unicode_digits.append(char)
                if len(self._unicode_digits) == 4:
                    self._append_codepoint(
                        int("".join(self._unicode_digits), 16), output
                    )
                    self._unicode_digits = None
                continue

            if self._escape:
                self._escape = False
                if char == "u":
                    self._unicode_digits = []
                    continue
                decoded = self._ESCAPES.get(char)
                if decoded is None:
                    self.error = "Invalid escape in streamed message envelope."
                    break
                if self._pending_high_surrogate is not None:
                    output.append("\ufffd")
                    self._pending_high_surrogate = None
                output.append(decoded)
                continue

            if char == "\\":
                self._escape = True
            elif char == '"':
                if self._pending_high_surrogate is not None:
                    output.append("\ufffd")
                    self._pending_high_surrogate = None
                self.done = True
            elif ord(char) < 0x20:
                self.error = "Unescaped control character in streamed message envelope."
                break
            else:
                if self._pending_high_surrogate is not None:
                    output.append("\ufffd")
                    self._pending_high_surrogate = None
                output.append(char)

        decoded = "".join(output)
        if decoded:
            self._parts.append(decoded)
        return self._visible_delta(decoded, final=self.done)


def _tool_catalog(
    tools: list[dict[str, Any]],
) -> dict[str, tuple[str, str, str | None]]:
    catalog: dict[str, tuple[str, str, str | None]] = {}
    for tool in tools:
        name = tool.get("name")
        tool_type = tool.get("type")
        if isinstance(name, str) and tool_type in {"function", "custom"}:
            wire_name = tool.get("_wire_name", name)
            namespace = tool.get("_namespace")
            if isinstance(wire_name, str) and (
                namespace is None or isinstance(namespace, str)
            ):
                catalog[name] = (tool_type, wire_name, namespace)
    return catalog


def collect_request_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect standard Responses tools and Codex `additional_tools` entries."""
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(candidate: Any, namespace: str | None = None) -> None:
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("name"), str
        ):
            return
        wire_name = candidate["name"]
        if not _SAFE_TOOL_NAME_RE.fullmatch(wire_name):
            return
        raw_type = candidate.get("type")
        if raw_type == "namespace":
            for child in candidate.get("tools") or []:
                add(child, wire_name)
            return
        public_name = f"{namespace}.{wire_name}" if namespace else wire_name
        if public_name in seen:
            return
        tool_type = "function" if raw_type == "function" else "custom"
        normalized = {**candidate, "name": public_name, "type": tool_type}
        if namespace:
            normalized["_namespace"] = namespace
            normalized["_wire_name"] = wire_name
        collected.append(normalized)
        seen.add(public_name)

    for candidate in body.get("tools") or []:
        add(candidate)

    input_value = body.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            # Codex extension shape can evolve. Only inspect list-valued fields on
            # the explicitly typed additional_tools item, and only accept named maps.
            for value in item.values():
                if isinstance(value, list):
                    for candidate in value:
                        add(candidate)

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


_COMPACT_DROP_KEYS = frozenset({"annotations", "logprobs"})
_COMPACT_ITEM_DROP_KEYS = frozenset(
    {"id", "status", "internal_chat_message_metadata_passthrough"}
)


def _compact_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _compact_value(item)
        for key, item in value.items()
        if key not in _COMPACT_DROP_KEYS
    }


def _compact_input_item(item: Any) -> Any | None:
    if not isinstance(item, dict):
        return item
    if item.get("type") == "additional_tools":
        return None

    item_type = item.get("type")
    compacted: dict[str, Any] = {}
    for key, value in item.items():
        if key in _COMPACT_DROP_KEYS:
            continue
        if key in _COMPACT_ITEM_DROP_KEYS and item_type in {"message", "reasoning"}:
            continue
        if key == "encrypted_content" and item_type == "reasoning":
            continue
        compacted[key] = _compact_value(value)

    if item_type == "reasoning":
        meaningful = {
            key: value
            for key, value in compacted.items()
            if key != "type" and value not in (None, "", [], {})
        }
        if not meaningful:
            return None
    return compacted


def _compact_input(value: Any) -> Any:
    if not isinstance(value, list):
        return _compact_value(value)
    compacted: list[Any] = []
    for item in value:
        result = _compact_input_item(item)
        if result is not None:
            compacted.append(result)
    return compacted


def _merged_request_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Move Codex additional_tools into one deduplicated prompt catalog."""
    configured = body.get("tools")
    merged = list(configured) if isinstance(configured, list) else []
    names = {
        item.get("name")
        for item in collect_request_tools({"tools": merged})
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for tool in collect_request_tools(body):
        name = tool.get("name")
        if name not in names:
            merged.append(tool)
            names.add(name)
    return merged


def _is_user_turn_boundary(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") in {None, "message"}
        and item.get("role") == "user"
    )


def _bridge_prompt_text(
    body: dict[str, Any],
    nonce: str,
    *,
    input_value: Any,
    tools: list[dict[str, Any]],
    continuation: bool,
    runtime_name: str,
    session_name: str,
    omitted_items: int,
    tool_batching: bool,
) -> str:
    begin = f"{_BRIDGE_PREFIX}_BEGIN_{nonce}"
    end = f"{_BRIDGE_PREFIX}_END_{nonce}"
    payload = {
        "instructions": body.get("instructions"),
        "input": input_value,
        "tools": tools,
        "tool_choice": body.get("tool_choice", "auto"),
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "reasoning": body.get("reasoning"),
        "max_output_tokens": body.get("max_output_tokens"),
        "text": body.get("text"),
    }
    if omitted_items:
        payload["history_truncation"] = {
            "applied": True,
            "omitted_input_items": omitted_items,
            "strategy": "oldest_complete_user_turns",
        }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if continuation:
        request_scope = (
            f"This is a continuation in the same {session_name}. Earlier bridge "
            "requests and your earlier bridge responses are already in the session. "
            "RESPONSE_REQUEST_JSON.input contains only the new items added since the "
            "last successful call. Continue from that state; do not repeat your "
            "previous response."
        )
    elif omitted_items:
        request_scope = (
            "Treat RESPONSE_REQUEST_JSON as the retained recent conversation for "
            "this new session. The bridge removed the oldest complete user turns "
            f"({omitted_items} compacted input items) to fit the bounded local "
            "transport. Do not claim to remember omitted details."
        )
    else:
        request_scope = (
            "Treat RESPONSE_REQUEST_JSON as the complete conversation for this "
            "new session."
        )
    message_example = '{"kind":"message","text":"your complete answer"}'
    tool_example = (
        '{"kind":"tool_calls","commentary":"brief user-visible progress update",'
        '"calls":['
        '{"name":"exact tool name","payload":{"arg":"value"}}]}'
    )
    parallel_tool_calls = body.get("parallel_tool_calls", True) is not False
    has_exec = any(
        tool.get("name") == "exec" and tool.get("type") == "custom"
        for tool in tools
        if isinstance(tool, dict)
    )
    if tool_batching:
        scheduling_rules = [
            "Minimize model/tool round trips. Before calling a tool, collect all "
            "independent reads or checks that are already known.",
            "Keep dependent operations sequential, and never parallelize conflicting "
            "writes, destructive actions, or separate approval-sensitive actions.",
            "After results arrive, do not repeat an equivalent tool call unless the "
            "previous result was incomplete, stale, or exposed a new gap.",
        ]
        if has_exec:
            scheduling_rules.insert(
                1,
                "The available exec custom tool is the preferred batching boundary: "
                "put independent nested calls in one raw JavaScript payload with "
                "await Promise.all([...]), await every promise, and emit compact "
                "results with text(...).",
            )
        if parallel_tool_calls:
            scheduling_rules.insert(
                2 if has_exec else 1,
                "When exec is not appropriate, return all independent top-level tool "
                "calls together in the same calls array.",
            )
        else:
            scheduling_rules.insert(
                2 if has_exec else 1,
                "parallel_tool_calls is false, so return at most one top-level tool "
                "call per envelope. One exec call may still coordinate independent "
                "nested calls concurrently inside its single JavaScript payload.",
            )
    else:
        scheduling_rules = [
            "Serial baseline mode is enabled for measurement. Return at most one "
            "top-level tool call per envelope.",
            "Do not combine independent nested operations with Promise.all or another "
            "concurrent batching mechanism.",
            "Keep each logical operation in a separate model/tool round, then continue "
            "after its result appears.",
        ]
    scheduling_text = "\n".join(f"- {rule}" for rule in scheduling_rules)

    return f"""You are the model inside a local OpenAI Responses API
compatibility bridge. Do not invoke {runtime_name}'s own filesystem, shell,
network, MCP, or other tools. The outer Codex client owns tool execution and
approval. {request_scope}
Follow the request instructions according to their roles.

A tool-call JSON envelope is inert protocol text: returning it delegates the
operation to outer Codex and does not execute the tool inside {runtime_name}.
When the request requires editing, deleting, shell execution, or another outer
tool action, return the appropriate tool-call envelope. Never tell the user to
switch {runtime_name} between Ask and Agent modes; do not perform the action
with {runtime_name}'s native tools.

Return exactly one JSON envelope between the two nonce markers, with no prose
outside them.

For a normal assistant answer:
{begin}
{message_example}
{end}

When an available tool is needed:
{begin}
{tool_example}
{end}

For every tool-call envelope, include a concise, truthful ``commentary`` update
before ``calls``. It is visible to the user as intermediate progress, not hidden
chain-of-thought. State what you are about to check or change and why, without
claiming that unfinished work is complete. Keep it to one or two short
sentences. For multi-step work, continue to provide a useful update before each
material tool round. If an ``update_plan`` tool is available either directly or
through the nested tools exposed by ``exec``, and the task has three or more
meaningful dependent steps, use its declared interface to create and maintain a
small plan with exactly one step in progress; do not invent plan state when that
tool is absent.

For a tool whose type is "function", payload must be a JSON object matching its
parameters. For a tool whose type is "custom", payload must be the exact string
input for that tool. Some Codex builds describe an implicit custom tool
(commonly "exec") inside instructions while omitting the top-level tools array.
You may call such a tool only when its name and FREEFORM input grammar are
explicitly described in RESPONSE_REQUEST_JSON; payload must then be the exact
raw string required by that grammar. For "exec", this is JavaScript source,
not a {{"cmd": ...}} object. For a namespaced tool, use the full
"namespace.tool_name" shown in the request. Never invent a tool name. After tool
output appears in a later request, either call another tool or return the final
message. The marker nonce is protocol data and must not appear inside JSON
fields.

Tool scheduling rules:
{scheduling_text}

RESPONSE_REQUEST_JSON
{serialized}
END_RESPONSE_REQUEST_JSON"""


def render_bridge_prompt(
    body: dict[str, Any],
    nonce: str,
    *,
    tool_source_body: dict[str, Any] | None = None,
    continuation: bool = False,
    runtime_name: str = "Kiro CLI",
    session_name: str = "Kiro session",
    tool_batching: bool = True,
    max_bytes: int | None = None,
) -> str:
    """Render a bounded prompt without truncating the newest complete turn.

    A resumed provider session receives only the new input suffix. Codex may put
    its tool catalog in an earlier ``additional_tools`` input item, so callers
    can supply the full logical request as ``tool_source_body`` while keeping
    the conversational input incremental.
    """
    compacted_input = _compact_input(body.get("input"))
    tool_body = body if tool_source_body is None else tool_source_body
    tools = _merged_request_tools(tool_body)
    prompt = _bridge_prompt_text(
        body,
        nonce,
        input_value=compacted_input,
        tools=tools,
        continuation=continuation,
        runtime_name=runtime_name,
        session_name=session_name,
        omitted_items=0,
        tool_batching=tool_batching,
    )
    if max_bytes is None or len(prompt.encode("utf-8")) <= max_bytes:
        return prompt

    if continuation or not isinstance(compacted_input, list):
        raise BridgePromptTooLargeError(len(prompt.encode("utf-8")), max_bytes)

    user_boundaries = [
        index
        for index, item in enumerate(compacted_input)
        if _is_user_turn_boundary(item)
    ]
    if not user_boundaries:
        raise BridgePromptTooLargeError(len(prompt.encode("utf-8")), max_bytes)
    preserved_prefix = compacted_input[: user_boundaries[0]]
    boundaries = user_boundaries[1:]
    best: str | None = None
    smallest_size = len(prompt.encode("utf-8"))
    low = 0
    high = len(boundaries) - 1
    while low <= high:
        middle = (low + high) // 2
        start = boundaries[middle]
        omitted_items = start - len(preserved_prefix)
        candidate = _bridge_prompt_text(
            body,
            nonce,
            input_value=[*preserved_prefix, *compacted_input[start:]],
            tools=tools,
            continuation=False,
            runtime_name=runtime_name,
            session_name=session_name,
            omitted_items=omitted_items,
            tool_batching=tool_batching,
        )
        candidate_size = len(candidate.encode("utf-8"))
        smallest_size = min(smallest_size, candidate_size)
        if candidate_size <= max_bytes:
            best = candidate
            high = middle - 1
        else:
            low = middle + 1
    if best is not None:
        return best
    raise BridgePromptTooLargeError(smallest_size, max_bytes)


def _contains_protocol_marker(value: Any) -> bool:
    if isinstance(value, str):
        return _BRIDGE_PREFIX in value
    if isinstance(value, list):
        return any(_contains_protocol_marker(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_protocol_marker(key) or _contains_protocol_marker(item)
            for key, item in value.items()
        )
    return False


def _decode_envelope(candidate: str) -> dict[str, Any]:
    try:
        value = _strict_json_loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BridgeProtocolError("Bridge protocol envelope is invalid.") from exc
    if not isinstance(value, dict):
        raise BridgeProtocolError("Bridge protocol envelope must be an object.")
    if value.get("kind") == "message" and isinstance(value.get("text"), str):
        _raise_for_upstream_status(value["text"])
    if _contains_protocol_marker(value):
        raise BridgeProtocolError("Nested bridge protocol data was rejected.")
    return value


def _find_envelope(output: str, nonce: str) -> dict[str, Any] | None:
    begin = f"{_BRIDGE_PREFIX}_BEGIN_{nonce}"
    end = f"{_BRIDGE_PREFIX}_END_{nonce}"
    begin_count = output.count(begin)
    end_count = output.count(end)
    if begin_count or end_count:
        if begin_count != 1 or end_count != 1:
            raise BridgeProtocolError("Duplicate or incomplete bridge markers.")
        start = output.find(begin)
        start += len(begin)
        finish = output.find(end, start)
        if finish < 0:
            raise BridgeProtocolError("Bridge protocol markers are out of order.")
        outside = output[: start - len(begin)] + output[finish + len(end) :]
        candidate = output[start:finish].strip()
        if _BRIDGE_PREFIX in outside:
            raise BridgeProtocolError("Nested or stale bridge markers were rejected.")
        return _decode_envelope(candidate)

    if _BRIDGE_PREFIX in output:
        raise BridgeProtocolError("Stale bridge protocol data was rejected.")

    candidate = output.strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            value = _strict_json_loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(value, dict):
            if _contains_protocol_marker(value):
                raise BridgeProtocolError("Nested bridge protocol data was rejected.")
            return value
    return None


def parse_bridge_output(
    output: str, tools: list[dict[str, Any]], nonce: str
) -> BridgeResult:
    """Parse one bridge envelope without ever treating control data as text."""
    _raise_for_upstream_status(output)
    envelope = _find_envelope(output, nonce)
    if envelope is None:
        return BridgeResult(text=output)

    kind = envelope.get("kind")
    if kind == "message":
        text = envelope.get("text")
        if isinstance(text, str):
            _raise_for_upstream_status(text)
            return BridgeResult(text=text)
        return BridgeResult(
            text="[provider-switchboard] Model returned an invalid message envelope."
        )

    if kind != "tool_calls" or not isinstance(envelope.get("calls"), list):
        return BridgeResult(
            text="[provider-switchboard] Model returned an unknown bridge envelope."
        )

    commentary_value = envelope.get("commentary")
    if commentary_value is None:
        commentary = None
    elif not isinstance(commentary_value, str) or not commentary_value.strip():
        raise BridgeProtocolError("Bridge commentary must be a non-empty string.")
    elif len(commentary_value) > _MAX_COMMENTARY_CHARS:
        raise BridgeProtocolError("Bridge commentary exceeded the safe size limit.")
    else:
        _raise_for_upstream_status(commentary_value)
        commentary = commentary_value

    catalog = _tool_catalog(tools)
    parsed: list[BridgeToolCall] = []
    if len(envelope["calls"]) > _MAX_TOOL_CALLS:
        return BridgeResult(text="[provider-switchboard] Too many tool calls.")
    for call in envelope["calls"]:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            return BridgeResult(
                text="[provider-switchboard] Model returned a malformed tool call."
            )
        name = call["name"]
        if not _SAFE_TOOL_NAME_RE.fullmatch(name):
            return BridgeResult(
                text="[provider-switchboard] Rejected invalid tool name."
            )
        tool_info = catalog.get(name)
        if tool_info is None:
            return BridgeResult(
                text=f"[provider-switchboard] Rejected unknown tool name: {name}"
            )
        tool_type, wire_name, namespace = tool_info

        if "payload" in call:
            payload: Any = call["payload"]
        elif tool_type == "function" and "arguments" in call:
            payload = call["arguments"]
        else:
            payload = call.get("input", "")

        if tool_type == "function":
            if isinstance(payload, str):
                try:
                    decoded_payload = _strict_json_loads(payload)
                except (json.JSONDecodeError, ValueError):
                    return BridgeResult(
                        text=f"{_ERROR_PREFIX} Invalid JSON arguments for tool: {name}"
                    )
                if not isinstance(decoded_payload, dict):
                    return BridgeResult(
                        text=f"{_ERROR_PREFIX} Tool arguments must be an object: {name}"
                    )
                encoded = payload
            elif isinstance(payload, dict):
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            else:
                return BridgeResult(
                    text=f"{_ERROR_PREFIX} Tool arguments must be an object: {name}"
                )
        else:
            try:
                encoded = (
                    payload
                    if isinstance(payload, str)
                    else json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError):
                return BridgeResult(
                    text=f"{_ERROR_PREFIX} Invalid payload for tool: {name}"
                )
        if len(encoded) > _MAX_TOOL_PAYLOAD_CHARS:
            return BridgeResult(
                text=f"[provider-switchboard] Tool payload is too large: {name}"
            )
        parsed.append(
            BridgeToolCall(
                name=wire_name,
                tool_type=tool_type,
                payload=encoded,
                namespace=namespace,
            )
        )

    if not parsed:
        return BridgeResult(
            text="[provider-switchboard] Model returned an empty tool call list."
        )
    return BridgeResult(
        text=None,
        tool_calls=tuple(parsed),
        commentary=commentary,
    )


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _message_item(text: str, phase: str) -> dict[str, Any]:
    return {
        "id": _id("msg"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "phase": phase,
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }


def output_items(result: BridgeResult) -> list[dict[str, Any]]:
    if result.text is not None:
        return [_message_item(result.text, "final_answer")]

    items: list[dict[str, Any]] = []
    if result.commentary is not None:
        items.append(_message_item(result.commentary, "commentary"))
    for call in result.tool_calls:
        if call.tool_type == "custom":
            items.append(
                {
                    "id": _id("ctc"),
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": _id("call"),
                    "name": call.name,
                    "input": call.payload,
                }
            )
        else:
            item = {
                "id": _id("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": _id("call"),
                "name": call.name,
                "arguments": call.payload,
            }
            if call.namespace:
                item["namespace"] = call.namespace
            items.append(item)
    return items


def response_object(
    body: dict[str, Any],
    model: str,
    response_id: str,
    status: str,
    items: list[dict[str, Any]],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    created_at = int(time.time())
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "completed_at": created_at if status == "completed" else None,
        "status": status,
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "max_tool_calls": body.get("max_tool_calls"),
        "model": model,
        "output": items,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": body.get("reasoning") or {"effort": None, "summary": None},
        "service_tier": "default",
        "store": False,
        "temperature": body.get("temperature"),
        "text": body.get("text") or {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_logprobs": body.get("top_logprobs", 0),
        "top_p": body.get("top_p"),
        "truncation": body.get("truncation", "disabled"),
        "usage": usage,
        "metadata": body.get("metadata") or {},
    }


def _chunks(value: str, size: int = 240) -> Iterable[str]:
    for start in range(0, len(value), size):
        yield value[start : start + size]


def output_item_events(
    item: dict[str, Any], output_index: int
) -> list[tuple[str, dict[str, Any]]]:
    """Build the Responses streaming lifecycle for one completed output item."""
    events: list[tuple[str, dict[str, Any]]] = []

    def add(event_type: str, **values: Any) -> None:
        events.append((event_type, values))

    pending = {**item, "status": "in_progress"}
    if item["type"] == "message":
        pending["content"] = []
    add("response.output_item.added", output_index=output_index, item=pending)

    if item["type"] == "message":
        text = item["content"][0]["text"]
        part = {"type": "output_text", "text": "", "annotations": []}
        add(
            "response.content_part.added",
            item_id=item["id"],
            output_index=output_index,
            content_index=0,
            part=part,
        )
        for delta in _chunks(text):
            add(
                "response.output_text.delta",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                delta=delta,
                logprobs=[],
            )
        add(
            "response.output_text.done",
            item_id=item["id"],
            output_index=output_index,
            content_index=0,
            text=text,
            logprobs=[],
        )
        add(
            "response.content_part.done",
            item_id=item["id"],
            output_index=output_index,
            content_index=0,
            part=item["content"][0],
        )
    elif item["type"] == "reasoning":
        summary = item.get("summary")
        text = ""
        if isinstance(summary, list):
            text = "\n".join(
                part.get("text", "")
                for part in summary
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        part = {"type": "summary_text", "text": ""}
        add(
            "response.reasoning_summary_part.added",
            item_id=item["id"],
            output_index=output_index,
            summary_index=0,
            part=part,
        )
        for delta in _chunks(text):
            add(
                "response.reasoning_summary_text.delta",
                item_id=item["id"],
                output_index=output_index,
                summary_index=0,
                delta=delta,
            )
        final_part = {"type": "summary_text", "text": text}
        add(
            "response.reasoning_summary_text.done",
            item_id=item["id"],
            output_index=output_index,
            summary_index=0,
            text=text,
        )
        add(
            "response.reasoning_summary_part.done",
            item_id=item["id"],
            output_index=output_index,
            summary_index=0,
            part=final_part,
        )
    elif item["type"] == "function_call":
        add(
            "response.function_call_arguments.delta",
            item_id=item["id"],
            output_index=output_index,
            delta=item["arguments"],
        )
        add(
            "response.function_call_arguments.done",
            item_id=item["id"],
            output_index=output_index,
            name=item["name"],
            arguments=item["arguments"],
        )
    elif item["type"] == "custom_tool_call":
        add(
            "response.custom_tool_call_input.delta",
            item_id=item["id"],
            output_index=output_index,
            delta=item["input"],
        )
        add(
            "response.custom_tool_call_input.done",
            item_id=item["id"],
            output_index=output_index,
            input=item["input"],
        )
    add("response.output_item.done", output_index=output_index, item=item)
    return events


def streaming_events(
    body: dict[str, Any],
    model: str,
    result: BridgeResult,
    _prompt: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response_id = _id("resp")
    completed_items = output_items(result)
    created = response_object(body, model, response_id, "in_progress", [], None)
    completed = response_object(
        body, model, response_id, "completed", completed_items, None
    )
    events: list[dict[str, Any]] = []

    def add(event_type: str, **values: Any) -> None:
        events.append({"type": event_type, "sequence_number": len(events), **values})

    add("response.created", response=created)
    add("response.in_progress", response=created)

    for output_index, item in enumerate(completed_items):
        for event_type, values in output_item_events(item, output_index):
            add(event_type, **values)

    add("response.completed", response=completed)
    return events, completed


def encode_sse(events: Iterable[dict[str, Any]]) -> Iterable[bytes]:
    for event in events:
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        yield f"event: {event['type']}\ndata: {data}\n\n".encode()


def request_summary(body: dict[str, Any]) -> dict[str, Any]:
    """Return content-free diagnostics suitable for local debug logs."""
    tools = body.get("tools") or []
    collected_tools = collect_request_tools(body)
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    input_value = body.get("input")
    input_shapes: list[dict[str, Any]] = []
    if isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                input_shapes.append({"python_type": type(item).__name__})
                continue
            shape: dict[str, Any] = {
                key: item.get(key)
                for key in ("type", "role", "name", "status")
                if key in item
            }
            content = item.get("content")
            if isinstance(content, list):
                shape["content_types"] = [
                    block.get("type")
                    if isinstance(block, dict)
                    else type(block).__name__
                    for block in content
                ]
            elif content is not None:
                shape["content_type"] = type(content).__name__
            if item.get("type") == "additional_tools":
                shape["keys"] = sorted(item)
                shape["list_fields"] = {
                    key: [
                        {
                            "name": value.get("name"),
                            "type": value.get("type"),
                            "keys": sorted(value),
                        }
                        for value in values
                        if isinstance(value, dict)
                    ]
                    for key, values in item.items()
                    if isinstance(values, list)
                }
            input_shapes.append(shape)
    encoded_input = json.dumps(body.get("input"), ensure_ascii=False)
    encoded_instructions = json.dumps(body.get("instructions"), ensure_ascii=False)
    reasoning = body.get("reasoning")
    client_metadata = body.get("client_metadata")
    reasoning_summary = (
        {key: reasoning.get(key) for key in ("effort", "summary") if key in reasoning}
        if isinstance(reasoning, dict)
        else None
    )
    return {
        "keys": sorted(body),
        "model": body.get("model"),
        "stream": body.get("stream"),
        "reasoning": reasoning_summary,
        "kiro_effort": kiro_effort_from_body(body),
        "codex_thread_key_hash": codex_thread_key_hash(body),
        "client_metadata_keys": (
            sorted(client_metadata) if isinstance(client_metadata, dict) else []
        ),
        "input_chars": len(encoded_input),
        "instructions_chars": len(encoded_instructions),
        "tool_count": len(tools),
        "top_level_tool_count": len(tools),
        "effective_tool_count": len(collected_tools),
        "tool_names": names,
        "collected_tools": [
            {"name": tool.get("name"), "type": tool.get("type")}
            for tool in collected_tools
        ],
        "input_shapes": input_shapes,
    }
