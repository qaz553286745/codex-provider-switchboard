from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .agent_loop_guard import (
    AGENT_ORCHESTRATION_GUIDANCE,
    has_agent_control_tools,
)
from .bridge import codex_thread_key_hash, collect_request_tools, kiro_effort_from_body

_ANTHROPIC_SIGNATURE_PREFIX = "switchboard:anthropic:"
_MAX_TOOL_RESULT_CHARS = 250_000
_KIRO_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIRO_TOOL_NAME_MAX_CHARS = 64
_TOOL_CALL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "tool_search_call",
        "mcp_tool_call",
    }
)
_TOOL_OUTPUT_ITEM_TYPES = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "tool_search_output",
        "mcp_tool_call_output",
    }
)
_SESSION_ID_RE = re.compile(r'["\']session_id["\']\s*:\s*["\']?(\d+)')
_WRITE_STDIN_SESSION_RE = re.compile(
    r"write_stdin\s*\(\s*\{.*?\bsession_id\s*:\s*(\d+)", re.DOTALL
)
_NUMERIC_EXIT_CODE_RE = re.compile(
    r'(?:["\']exit_code["\']\s*:\s*|\bexit_code\s*=\s*)-?\d+'
)
_LOST_EXIT_CODE_RE = re.compile(
    r'(?:["\']exit_code["\']\s*:\s*(?:null|["\']?undefined["\']?)'
    r"|\bexit_code\s*=\s*(?:null|undefined))",
    re.IGNORECASE,
)
_PROCESS_GONE_MARKERS = (
    "unknown process id",
    "process not found",
    "no such process",
    "session is no longer available",
)
_EXEC_BACKGROUND_GUIDANCE = """
Codex background-command continuation contract:
- When JavaScript calls a nested ``tools.exec_command(...)``, preserve the full
  result with ``text(JSON.stringify(result))``. Printing only ``result.output``
  or ``result.exit_code`` can discard the live ``session_id``.
- If the result contains ``session_id`` and no numeric ``exit_code``, the command
  is still running. Do not launch the same command again and do not merely list
  ``ALL_TOOLS``. Continue it through this same exec tool by calling
  ``await tools.write_stdin({session_id: ID, chars: "", yield_time_ms: 30000,
  max_output_tokens: 30000})`` and serialize that complete result.
- Repeat polling while a ``session_id`` is returned. Only use the final command
  result after a numeric ``exit_code`` is present.
""".strip()


def _with_agent_guidance(system: str, tools: list[dict[str, Any]]) -> str:
    if not has_agent_control_tools(tools):
        return system
    guidance = AGENT_ORCHESTRATION_GUIDANCE.strip()
    return f"{system}\n\n{guidance}" if system else guidance


@dataclass(frozen=True, slots=True)
class ToolContinuationState:
    """Local Codex tool sessions that still need a client-owned continuation."""

    pending_session_ids: tuple[int, ...] = ()
    lost_session_handle: bool = False


@dataclass(slots=True)
class ConversationMessage:
    role: str
    content: list[dict[str, Any]] = field(default_factory=list)
    kind: str | None = None


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and block.get("type") in {
                "input_text",
                "output_text",
                "text",
                None,
            }:
                parts.append(text)
    return "".join(parts)


def _tool_catalog(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(tool["name"]): tool for tool in collect_request_tools(body)}


def _kiro_tool_alias(public_name: str, used: set[str]) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", public_name).strip("_") or "tool"
    digest = hashlib.sha256(public_name.encode()).hexdigest()[:12]
    counter = 0
    while True:
        collision_suffix = "" if counter == 0 else f"_{counter:x}"
        suffix = f"_{digest}{collision_suffix}"
        stem_chars = max(1, _KIRO_TOOL_NAME_MAX_CHARS - len(suffix))
        candidate = f"{stem[:stem_chars]}{suffix}"[:_KIRO_TOOL_NAME_MAX_CHARS]
        if candidate not in used:
            return candidate
        counter += 1


def _kiro_tool_catalog(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tools = collect_request_tools(body)
    used = {
        str(tool["name"])
        for tool in tools
        if _KIRO_TOOL_NAME_RE.fullmatch(str(tool["name"]))
    }
    catalog: dict[str, dict[str, Any]] = {}
    for tool in tools:
        public_name = str(tool["name"])
        alias = (
            public_name
            if _KIRO_TOOL_NAME_RE.fullmatch(public_name)
            else _kiro_tool_alias(public_name, used)
        )
        catalog[alias] = tool
        used.add(alias)
    return catalog


def _tool_result_text(value: Any) -> str:
    text = _content_text(value)
    if not text:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                text = "[unavailable tool result]"
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    half = _MAX_TOOL_RESULT_CHARS // 2
    return f"{text[:half]}\n... [TRUNCATED] ...\n{text[-half:]}"


def _tool_call_name(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "tool_search_call":
        return "tool_search"
    if item_type == "local_shell_call":
        return "local_shell"
    name = item.get("name")
    if not isinstance(name, str):
        return ""
    if item_type == "mcp_tool_call":
        server_label = item.get("server_label")
        if isinstance(server_label, str) and server_label:
            return f"{server_label}.{name}"
    namespace = item.get("namespace")
    if isinstance(namespace, str) and not name.startswith(f"{namespace}."):
        return f"{namespace}.{name}"
    return name


def _tool_call_input(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type in {"function_call", "tool_search_call", "mcp_tool_call"}:
        raw = item.get("arguments")
    elif item_type == "local_shell_call":
        raw = item.get("action")
    else:
        raw = item.get("input")
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    try:
        return json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return ""


def _is_exec_name(name: str) -> bool:
    return name == "exec" or name.endswith(".exec")


def _tool_output_value(item: dict[str, Any]) -> Any:
    output = item.get("output")
    return item.get("input") if output is None else output


def analyze_tool_continuation(body: dict[str, Any]) -> ToolContinuationState:
    """Recover running nested exec sessions from Codex Responses history.

    Codex owns local tool execution. Switchboard therefore cannot wait on a
    process directly, but it can preserve the client session handle and emit a
    safe follow-up exec call that invokes ``tools.write_stdin``.
    """

    input_value = body.get("input")
    if not isinstance(input_value, list):
        return ToolContinuationState()

    calls: dict[str, tuple[str, str, int | None]] = {}
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") not in _TOOL_CALL_ITEM_TYPES:
            continue
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str):
            continue
        name = _tool_call_name(item)
        call_input = _tool_call_input(item)
        match = _WRITE_STDIN_SESSION_RE.search(call_input)
        calls[call_id] = (
            name,
            call_input,
            int(match.group(1)) if match is not None else None,
        )

    pending: set[int] = set()
    lost_session_handle = False
    for item in input_value:
        if (
            not isinstance(item, dict)
            or item.get("type") not in _TOOL_OUTPUT_ITEM_TYPES
        ):
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        call = calls.get(call_id)
        if call is None or not _is_exec_name(call[0]):
            continue

        text = _tool_result_text(_tool_output_value(item))
        normalized = text.casefold()
        target_session = call[2]
        terminal = (
            item.get("status") == "failed"
            or _NUMERIC_EXIT_CODE_RE.search(text) is not None
            or any(marker in normalized for marker in _PROCESS_GONE_MARKERS)
        )
        if target_session is not None and terminal:
            pending.discard(target_session)

        returned_sessions = {int(value) for value in _SESSION_ID_RE.findall(text)}
        if not terminal:
            pending.update(returned_sessions)
        if _LOST_EXIT_CODE_RE.search(text) is not None and not returned_sessions:
            lost_session_handle = True

    return ToolContinuationState(
        pending_session_ids=tuple(sorted(pending)),
        lost_session_handle=lost_session_handle,
    )


def _is_exec_tool(tool: dict[str, Any]) -> bool:
    name = tool.get("_wire_name") or tool.get("name")
    return tool.get("type") == "custom" and isinstance(name, str) and name == "exec"


def find_exec_tool_alias(catalog: dict[str, dict[str, Any]]) -> str | None:
    for alias, tool in catalog.items():
        if _is_exec_tool(tool):
            return alias
    return None


def _direct_tool_description(tool: dict[str, Any]) -> str:
    description = str(tool.get("description") or "").strip()
    if not _is_exec_tool(tool):
        return description[:10_000]
    joined = (
        f"{description}\n\n{_EXEC_BACKGROUND_GUIDANCE}"
        if description
        else _EXEC_BACKGROUND_GUIDANCE
    )
    return joined[:10_000]


def _with_tool_continuation_guidance(
    system: str,
    body: dict[str, Any],
    tools: list[dict[str, Any]],
) -> str:
    if not any(_is_exec_tool(tool) for tool in tools):
        return system

    parts = (
        [system, _EXEC_BACKGROUND_GUIDANCE] if system else [_EXEC_BACKGROUND_GUIDANCE]
    )
    state = analyze_tool_continuation(body)
    if state.pending_session_ids:
        session_list = ", ".join(str(value) for value in state.pending_session_ids)
        parts.append(
            "Switchboard detected running Codex exec session(s): "
            f"{session_list}. The server will schedule safe write_stdin polling "
            "before another model action; never relaunch their commands."
        )
    if state.lost_session_handle:
        parts.append(
            "A prior exec result exposed exit_code=undefined without preserving a "
            "session_id. Treat that command as potentially still running. Inspect "
            "its process state before considering one replacement, and never start "
            "multiple identical replacements."
        )
    return "\n\n".join(part for part in parts if part)


def _decode_anthropic_signature(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith(_ANTHROPIC_SIGNATURE_PREFIX):
        return None
    encoded = value[len(_ANTHROPIC_SIGNATURE_PREFIX) :]
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def encode_anthropic_signature(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return f"{_ANTHROPIC_SIGNATURE_PREFIX}{encoded}"


def _append_message(
    messages: list[ConversationMessage], role: str, blocks: list[dict[str, Any]]
) -> None:
    if not blocks:
        return
    if messages and messages[-1].role == role:
        messages[-1].content.extend(blocks)
    else:
        messages.append(ConversationMessage(role=role, content=blocks))


def _agent_identity(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    printable = "".join(char for char in value.strip() if ord(char) >= 0x20)
    return printable[:200] or "unknown"


def _agent_message(item: dict[str, Any]) -> ConversationMessage | None:
    """Turn Codex inter-agent envelopes into an explicit active user message.

    Recent Codex builds place the actual delegated task in an
    ``encrypted_content`` block on ``agent_message`` items.  That field is opaque
    on reasoning items, so it is deliberately decoded as text only for this one
    item type.
    """

    content = item.get("content")
    legacy_encrypted: list[str] = []
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "output_text", "text"} and isinstance(
                block.get("text"), str
            ):
                parts.append(block["text"])
            elif block_type == "encrypted_content" and isinstance(
                block.get("encrypted_content"), str
            ):
                legacy_encrypted.append(block["encrypted_content"])
    else:
        return None

    visible_payload = "\n".join(part for part in parts if part)
    # Older Codex clients used this field as a plainly encoded envelope segment.
    # Official multi-agent Responses treats it as opaque, so only consume the
    # legacy segment when a visible message marker explicitly identifies it.
    if "Message Type:" in visible_payload:
        parts.extend(legacy_encrypted)
    payload = "\n".join(part for part in parts if part)
    if not payload:
        return None
    if "Message Type: NEW_TASK" in payload:
        kind = "agent_new_task"
        directive = (
            "ACTIVE CODEX SUBAGENT TASK: This is the current task for this agent. "
            "Treat inherited conversation turns as background only and do not "
            "continue an older parent task unless this message explicitly asks "
            "you to do so."
        )
    elif "Message Type: FINAL_ANSWER" in payload:
        kind = "agent_final_answer"
        directive = (
            "CODEX SUBAGENT RESULT: Treat this as the named child agent's result "
            "for the current orchestration turn."
        )
    else:
        kind = "agent_message"
        directive = "CODEX INTER-AGENT MESSAGE: Process this as the current message."

    author = _agent_identity(item.get("author"))
    recipient = _agent_identity(item.get("recipient"))
    text = f"{directive}\nAuthor: {author}\nRecipient: {recipient}\n\n{payload}"
    return ConversationMessage(
        role="user",
        content=[{"type": "text", "text": text}],
        kind=kind,
    )


def responses_conversation(body: dict[str, Any]) -> list[ConversationMessage]:
    """Normalize Responses input into user/assistant content blocks."""
    input_value = body.get("input")
    if isinstance(input_value, str):
        return [
            ConversationMessage(
                role="user", content=[{"type": "text", "text": input_value}]
            )
        ]
    if not isinstance(input_value, list):
        return []

    messages: list[ConversationMessage] = []
    pending_tools: list[dict[str, Any]] = []
    for item in input_value:
        if isinstance(item, str):
            _append_message(messages, "user", [{"type": "text", "text": item}])
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = item.get("role")
        if item_type == "agent_message":
            if pending_tools:
                _append_message(messages, "assistant", pending_tools)
                pending_tools = []
            agent_message = _agent_message(item)
            if agent_message is not None:
                # Preserve the envelope boundary instead of merging it into an
                # inherited user turn.  Kiro uses this boundary to select the
                # active child task below.
                messages.append(agent_message)
            continue
        if item_type == "message" or role in {
            "user",
            "assistant",
            "system",
            "developer",
        }:
            if role in {"system", "developer"}:
                continue
            blocks: list[dict[str, Any]] = []
            content = item.get("content")
            if isinstance(content, str):
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        blocks.append({"type": "text", "text": block})
                        continue
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type in {
                        "input_text",
                        "output_text",
                        "text",
                    } and isinstance(block.get("text"), str):
                        blocks.append({"type": "text", "text": block["text"]})
                    elif block_type in {"input_image", "image"}:
                        image_url = block.get("image_url") or block.get("url")
                        if isinstance(image_url, str):
                            blocks.append({"type": "image", "url": image_url})
            _append_message(
                messages, "assistant" if role == "assistant" else "user", blocks
            )
            continue
        if item_type == "reasoning":
            signature = _decode_anthropic_signature(item.get("encrypted_content"))
            if signature:
                summary = item.get("summary")
                thinking = ""
                if isinstance(summary, list):
                    thinking = "\n".join(
                        str(part.get("text"))
                        for part in summary
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                _append_message(
                    messages,
                    "assistant",
                    [
                        {
                            "type": "thinking",
                            "thinking": thinking,
                            "signature": signature,
                        }
                    ],
                )
            continue
        if item_type in _TOOL_CALL_ITEM_TYPES:
            name = _tool_call_name(item)
            call_id = item.get("call_id") or item.get("id")
            if not name or not isinstance(call_id, str):
                continue
            raw = _tool_call_input(item)
            if isinstance(raw, str):
                try:
                    arguments = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    arguments = {"input": raw}
            elif isinstance(raw, dict):
                arguments = raw
            else:
                arguments = {}
            pending_tools.append(
                {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
            )
            continue
        if item_type in _TOOL_OUTPUT_ITEM_TYPES:
            if pending_tools:
                _append_message(messages, "assistant", pending_tools)
                pending_tools = []
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            output = item.get("output")
            if output is None:
                output = item.get("input")
            _append_message(
                messages,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": _tool_result_text(output),
                        "is_error": item.get("status") == "failed",
                    }
                ],
            )
    if pending_tools:
        _append_message(messages, "assistant", pending_tools)
    return messages


def _image_source(url: str) -> dict[str, Any] | None:
    if not url.startswith("data:image/") or ";base64," not in url:
        return None
    header, data = url.split(",", 1)
    media_type = header[5:].split(";", 1)[0]
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        return None
    if len(data) > 20 * 1_048_576:
        return None
    return {"type": "base64", "media_type": media_type, "data": data}


def responses_to_anthropic(
    body: dict[str, Any], model: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog = _tool_catalog(body)
    messages: list[dict[str, Any]] = []
    for message in responses_conversation(body):
        content: list[dict[str, Any]] = []
        for block in message.content:
            block_type = block.get("type")
            if block_type == "text":
                content.append({"type": "text", "text": str(block.get("text") or "")})
            elif block_type == "image" and message.role == "user":
                source = _image_source(str(block.get("url") or ""))
                if source:
                    content.append({"type": "image", "source": source})
            elif block_type in {"tool_use", "tool_result", "thinking"}:
                content.append(dict(block))
        if content:
            messages.append({"role": message.role, "content": content})
    if not messages:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Continue."}]}
        ]
    elif messages[0]["role"] != "user":
        messages.insert(
            0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]}
        )

    tools: list[dict[str, Any]] = []
    for public_name, tool in catalog.items():
        if tool.get("type") in {"function", "tool_search"}:
            schema = tool.get("parameters")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
        else:
            schema = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        tools.append(
            {
                "name": public_name,
                "description": _direct_tool_description(tool),
                "input_schema": schema,
            }
        )

    max_tokens = body.get("max_output_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = 32_768
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": min(max_tokens, 128_000),
        "stream": True,
    }
    instructions = body.get("instructions")
    system = instructions if isinstance(instructions, str) else ""
    system = _with_agent_guidance(system, list(catalog.values()))
    system = _with_tool_continuation_guidance(system, body, list(catalog.values()))
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    tool_choice = body.get("tool_choice")
    if tool_choice in {"auto", "none", "any"}:
        payload["tool_choice"] = {"type": tool_choice}
    effort = kiro_effort_from_body(body)
    if effort in {"low", "medium", "high", "max"}:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": effort}
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    return payload, catalog


def responses_to_kiro(
    body: dict[str, Any], model: str, *, conversation_id: str | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog = _kiro_tool_catalog(body)
    kiro_name_by_public = {str(tool["name"]): alias for alias, tool in catalog.items()}
    normalized = responses_conversation(body)
    instructions = body.get("instructions")
    system = instructions if isinstance(instructions, str) else ""
    system = _with_agent_guidance(system, list(catalog.values()))
    system = _with_tool_continuation_guidance(system, body, list(catalog.values()))
    effort = kiro_effort_from_body(body)
    if effort:
        budget = {"low": 10_000, "medium": 20_000, "high": 30_000}.get(effort, 50_000)
        system = (
            f"<thinking_mode>enabled</thinking_mode>"
            f"<max_thinking_length>{budget}</max_thinking_length>"
            + (f"\n{system}" if system else "")
        )

    history: list[dict[str, Any]] = []
    current_index = max(0, len(normalized) - 1)
    while current_index > 0 and normalized[current_index].role == "user":
        current_index -= 1
        if normalized[current_index].role == "assistant":
            current_index += 1
            break
    # A NEW_TASK/FINAL_ANSWER envelope at the end of a Codex turn is a hard
    # current-message boundary.  Without this, adjacent inherited user messages
    # are concatenated and Kiro may resume the parent's older task.
    for index in range(current_index, len(normalized)):
        if normalized[index].kind in {
            "agent_new_task",
            "agent_final_answer",
            "agent_message",
        }:
            current_index = index
    historical = normalized[:current_index]
    current = normalized[current_index:] or [
        ConversationMessage("user", [{"type": "text", "text": "Continue."}])
    ]
    system_used = False

    def user_wire(
        blocks: list[dict[str, Any]], *, include_tools: bool
    ) -> dict[str, Any]:
        nonlocal system_used
        texts: list[str] = []
        results: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                results.append(
                    {
                        "content": [{"text": str(block.get("content") or "")}],
                        "status": "error" if block.get("is_error") else "success",
                        "toolUseId": str(block.get("tool_use_id") or ""),
                    }
                )
            elif block.get("type") == "image":
                source = _image_source(str(block.get("url") or ""))
                if source:
                    images.append(
                        {
                            "format": str(source["media_type"]).split("/", 1)[1],
                            "source": {"bytes": source["data"]},
                        }
                    )
        text = "\n\n".join(part for part in texts if part) or "Tool results provided."
        if system and not system_used:
            text = f"{system}\n\n{text}"
            system_used = True
        value: dict[str, Any] = {
            "content": text,
            "modelId": model,
            "origin": "KIRO_CLI",
        }
        if images:
            value["images"] = images
        context: dict[str, Any] = {}
        if results:
            context["toolResults"] = results
        if include_tools and catalog:
            context["tools"] = [
                {
                    "toolSpecification": {
                        "name": public_name,
                        "description": _direct_tool_description(tool),
                        "inputSchema": {
                            "json": (
                                tool.get("parameters")
                                if tool.get("type") in {"function", "tool_search"}
                                and isinstance(tool.get("parameters"), dict)
                                else {
                                    "type": "object",
                                    "properties": {"input": {"type": "string"}},
                                    "required": ["input"],
                                }
                            )
                        },
                    }
                }
                for public_name, tool in catalog.items()
            ]
        if context:
            value["userInputMessageContext"] = context
        return value

    for message in historical:
        if message.role == "user":
            history.append(
                {"userInputMessage": user_wire(message.content, include_tools=False)}
            )
        else:
            text_parts: list[str] = []
            tool_uses: list[dict[str, Any]] = []
            for block in message.content:
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block.get("type") == "thinking":
                    text_parts.insert(
                        0, f"<thinking>{block.get('thinking') or ''}</thinking>"
                    )
                elif block.get("type") == "tool_use":
                    public_name = str(block.get("name") or "")
                    kiro_name = kiro_name_by_public.get(public_name)
                    if kiro_name is None:
                        kiro_name = (
                            public_name
                            if _KIRO_TOOL_NAME_RE.fullmatch(public_name)
                            else _kiro_tool_alias(public_name, set(catalog))
                        )
                    tool_uses.append(
                        {
                            "name": kiro_name,
                            "toolUseId": block.get("id"),
                            "input": block.get("input") or {},
                        }
                    )
            response: dict[str, Any] = {"content": "\n\n".join(text_parts)}
            if tool_uses:
                response["toolUses"] = tool_uses
            if response["content"] or tool_uses:
                history.append({"assistantResponseMessage": response})

    current_blocks: list[dict[str, Any]] = []
    for message in current:
        current_blocks.extend(message.content)
    current_message = user_wire(current_blocks, include_tools=True)
    thread_hash = codex_thread_key_hash(body)
    value: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "agentTaskType": "vibe",
            "conversationId": conversation_id or thread_hash or str(uuid.uuid4()),
            "currentMessage": {"userInputMessage": current_message},
        },
        "agentMode": "vibe",
    }
    if history:
        value["conversationState"]["history"] = history
    return value, catalog


def message_item(text: str, *, phase: str) -> dict[str, Any]:
    return {
        "id": _identifier("msg"),
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


def reasoning_item(text: str, *, signature: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": _identifier("rs"),
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": text}],
    }
    if signature:
        value["encrypted_content"] = encode_anthropic_signature(signature)
    return value


def tool_item(
    name: str,
    arguments: Any,
    call_id: str,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    tool = catalog.get(name)
    if tool is None:
        return None
    wire_name = tool.get("_wire_name") or tool.get("name") or name
    namespace = tool.get("_namespace")
    if tool.get("type") == "tool_search":
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                decoded = {}
        else:
            decoded = arguments
        if not isinstance(decoded, dict):
            decoded = {}
        return {
            "id": _identifier("tsc"),
            "type": "tool_search_call",
            "status": "completed",
            "call_id": call_id,
            "arguments": decoded,
            "execution": "client",
        }
    if tool.get("type") == "custom":
        if isinstance(arguments, dict) and isinstance(arguments.get("input"), str):
            text = arguments["input"]
        elif isinstance(arguments, str):
            text = arguments
        else:
            text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        return {
            "id": _identifier("ctc"),
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": call_id,
            "name": wire_name,
            "input": text,
        }
    encoded = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
    )
    value = {
        "id": _identifier("fc"),
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": wire_name,
        "arguments": encoded,
    }
    if isinstance(namespace, str):
        value["namespace"] = namespace
    return value


def responses_usage(
    *, input_tokens: int = 0, output_tokens: int = 0, reasoning_tokens: int = 0
) -> dict[str, Any]:
    return {
        "input_tokens": max(0, input_tokens),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": max(0, output_tokens),
        "output_tokens_details": {"reasoning_tokens": max(0, reasoning_tokens)},
        "total_tokens": max(0, input_tokens) + max(0, output_tokens),
    }
