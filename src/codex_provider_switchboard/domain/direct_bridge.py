from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .bridge import codex_thread_key_hash, collect_request_tools, kiro_effort_from_body

_ANTHROPIC_SIGNATURE_PREFIX = "switchboard:anthropic:"
_MAX_TOOL_RESULT_CHARS = 250_000
_KIRO_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIRO_TOOL_NAME_MAX_CHARS = 64


@dataclass(slots=True)
class ConversationMessage:
    role: str
    content: list[dict[str, Any]] = field(default_factory=list)


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
        if item_type in {"function_call", "custom_tool_call"}:
            name = item.get("name")
            call_id = item.get("call_id") or item.get("id")
            if not isinstance(name, str) or not isinstance(call_id, str):
                continue
            namespace = item.get("namespace")
            if isinstance(namespace, str) and not name.startswith(f"{namespace}."):
                name = f"{namespace}.{name}"
            raw = (
                item.get("arguments")
                if item_type == "function_call"
                else item.get("input")
            )
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
        if item_type in {"function_call_output", "custom_tool_call_output"}:
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
        if tool.get("type") == "function":
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
                "description": str(tool.get("description") or "")[:10_000],
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
    if isinstance(instructions, str) and instructions:
        payload["system"] = instructions
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
    effort = kiro_effort_from_body(body)
    if effort:
        budget = {"low": 10_000, "medium": 20_000, "high": 30_000}.get(effort, 50_000)
        system = (
            f"<thinking_mode>enabled</thinking_mode>"
            f"<max_thinking_length>{budget}</max_thinking_length>"
            + (f"\n{system}" if system else "")
        )

    history: list[dict[str, Any]] = []
    current_index = len(normalized) - 1
    while current_index > 0 and normalized[current_index].role == "user":
        current_index -= 1
        if normalized[current_index].role == "assistant":
            current_index += 1
            break
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
                        "description": str(tool.get("description") or "")[:10_000],
                        "inputSchema": {
                            "json": (
                                tool.get("parameters")
                                if tool.get("type") == "function"
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
