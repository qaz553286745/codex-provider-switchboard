from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

from ..application.inspector import RequestInspector
from ..domain.direct_bridge import (
    encode_anthropic_signature,
    responses_to_anthropic,
    responses_to_kiro,
    responses_usage,
    tool_item,
)
from ..infrastructure.config_store import ConfigStore
from ..infrastructure.direct_catalog import direct_platform
from ..infrastructure.direct_client import DirectAPIError, DirectClient
from ._streaming import ResponseEventStream
from .base import ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

_KIRO_FINAL_TOOL_PREFIX = "switchboard_submit_final_answer"
_KIRO_MAX_ATTEMPTS = 2
_KIRO_CONTINUATION_PROMPT = (
    "Your previous response ended without either requesting a tool or submitting "
    "the final answer. Continue the task now. Do not repeat progress already "
    "reported. Use a normal tool if more work is required; otherwise submit the "
    "complete answer with the Switchboard final-answer tool."
)


def _install_kiro_completion_protocol(
    payload: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Require an unambiguous terminal action from an agentic Kiro response."""
    name = _KIRO_FINAL_TOOL_PREFIX
    suffix = 2
    while name in catalog:
        name = f"{_KIRO_FINAL_TOOL_PREFIX}_{suffix}"
        suffix += 1

    state = payload.get("conversationState")
    current = state.get("currentMessage") if isinstance(state, dict) else None
    user = current.get("userInputMessage") if isinstance(current, dict) else None
    if not isinstance(user, dict):
        raise DirectAPIError("Kiro request did not contain a current user message.")
    context = user.setdefault("userInputMessageContext", {})
    if not isinstance(context, dict):
        raise DirectAPIError("Kiro request contained an invalid user context.")
    tools = context.setdefault("tools", [])
    if not isinstance(tools, list):
        raise DirectAPIError("Kiro request contained an invalid tool catalog.")
    tools.append(
        {
            "toolSpecification": {
                "name": name,
                "description": (
                    "Required terminal action for a completed turn. Plain assistant "
                    "text is progress commentary and never completes the turn. If "
                    "more work is required, call a normal tool. Only when the user's "
                    "task is fully finished, call this tool exactly once with the "
                    "complete user-facing final answer. Do not use it for progress."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "final_answer": {
                                "type": "string",
                                "description": "Complete user-facing final answer.",
                            }
                        },
                        "required": ["final_answer"],
                        "additionalProperties": False,
                    }
                },
            }
        }
    )
    return name, deepcopy(current)


def _kiro_continuation_payload(
    payload: dict[str, Any],
    previous_current: dict[str, Any],
    assistant_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the next Kiro turn without losing prior progress or tool access."""
    value = deepcopy(payload)
    state = value["conversationState"]
    history = state.setdefault("history", [])
    historical_user = deepcopy(previous_current)
    historical_message = historical_user.get("userInputMessage")
    if isinstance(historical_message, dict):
        context = historical_message.get("userInputMessageContext")
        if isinstance(context, dict):
            context.pop("tools", None)
            if not context:
                historical_message.pop("userInputMessageContext", None)
    history.append(historical_user)
    history.append({"assistantResponseMessage": {"content": assistant_text[-64_000:]}})
    next_current = deepcopy(state["currentMessage"])
    next_user = next_current["userInputMessage"]
    next_user["content"] = _KIRO_CONTINUATION_PROMPT
    state["currentMessage"] = next_current
    return value, deepcopy(next_current)


class _ThinkingTags:
    def __init__(self) -> None:
        self.buffer = ""
        self.mode = "text"

    def feed(self, value: str, *, final: bool = False) -> list[tuple[str, str]]:
        self.buffer += value
        result: list[tuple[str, str]] = []
        while self.buffer:
            marker = "<thinking>" if self.mode == "text" else "</thinking>"
            index = self.buffer.find(marker)
            if index >= 0:
                if index:
                    result.append((self.mode, self.buffer[:index]))
                self.buffer = self.buffer[index + len(marker) :]
                self.mode = "thinking" if self.mode == "text" else "text"
                continue
            if final:
                result.append((self.mode, self.buffer))
                self.buffer = ""
                break
            retained = 0
            maximum = min(len(self.buffer), len(marker) - 1)
            for size in range(maximum, 0, -1):
                if self.buffer.endswith(marker[:size]):
                    retained = size
                    break
            split_at = len(self.buffer) - retained
            if split_at:
                result.append((self.mode, self.buffer[:split_at]))
                self.buffer = self.buffer[split_at:]
            break
        return result


def _failed_sse(message: str, *, code: str = "direct_provider_error") -> bytes:
    value = {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {
            "id": f"resp_{os.urandom(16).hex()}",
            "object": "response",
            "status": "failed",
            "error": {"code": code, "message": message[:1_000]},
        },
    }
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"event: response.failed\ndata: {data}\n\n".encode()


def _decode_single_event(chunk: bytes) -> dict[str, Any] | None:
    data_lines: list[str] = []
    try:
        lines = chunk.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    for line in lines:
        if line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if not data_lines:
        return None
    try:
        value = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class DirectProvider:
    provider_id = "direct"

    def __init__(
        self,
        store: ConfigStore,
        client: DirectClient,
        inspector: RequestInspector,
    ) -> None:
        self.store = store
        self.client = client
        self.inspector = inspector
        self.last_usage: dict[str, Any] | None = None

    def platform_id(self) -> str:
        return str(self.store.read()["direct"]["platform_id"])

    def model_id(self) -> str:
        return str(self.store.read()["direct"]["model_id"])

    def _upstream_body(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.store.read()["direct"].get("follow_codex_effort", True):
            return body
        value = dict(body)
        value.pop("reasoning", None)
        value.pop("reasoning_effort", None)
        return value

    def _record(self, action: str) -> None:
        self.inspector.record(
            provider=self.provider_id,
            action=f"{self.platform_id()}_{action}",
            model=self.model_id(),
            session_reused=False,
        )

    async def complete(self, body: dict[str, Any]) -> ProviderResponse:
        terminal: dict[str, Any] | None = None
        async for chunk in self.stream({**body, "stream": True}):
            event = _decode_single_event(chunk)
            if not event:
                continue
            event_type = event.get("type")
            if event_type == "response.completed" and isinstance(
                event.get("response"), dict
            ):
                terminal = event["response"]
            elif event_type == "response.failed":
                response = event.get("response")
                error = response.get("error") if isinstance(response, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                raise ProviderError(
                    str(message or "Direct provider request failed."),
                    error_type="direct_provider_error",
                    status_code=502,
                )
        if terminal is None:
            raise ProviderError(
                "Direct provider stream ended without response.completed.",
                error_type="direct_protocol_error",
                status_code=502,
            )
        usage = terminal.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else None
        return ProviderResponse(
            terminal,
            {
                "X-Switchboard-Provider": self.provider_id,
                "X-Switchboard-Platform": self.platform_id(),
            },
        )

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        self._record("responses_stream")
        platform = direct_platform(self.platform_id())
        upstream_body = self._upstream_body(body)
        try:
            if platform.protocol == "responses":
                async for chunk in self.client.stream_responses(upstream_body):
                    event = _decode_single_event(chunk)
                    if event and event.get("type") == "response.completed":
                        response = event.get("response")
                        usage = (
                            response.get("usage")
                            if isinstance(response, dict)
                            else None
                        )
                        self.last_usage = (
                            dict(usage) if isinstance(usage, dict) else None
                        )
                    yield chunk
            elif platform.protocol == "anthropic":
                async for chunk in self._stream_anthropic(upstream_body):
                    yield chunk
            elif platform.protocol == "kiro":
                async for chunk in self._stream_kiro(upstream_body):
                    yield chunk
            else:  # pragma: no cover - catalog typing prevents this.
                raise DirectAPIError("Unsupported direct-provider protocol.")
        except DirectAPIError as exc:
            yield _failed_sse(str(exc))
        except (TypeError, ValueError, RecursionError) as exc:
            error_name = type(exc).__name__
            yield _failed_sse(
                f"Direct-provider request could not be translated ({error_name}).",
                code="direct_translation_error",
            )
        except Exception as exc:
            logger.exception(
                "Unexpected Direct-provider request failure error_type=%s",
                type(exc).__name__,
            )
            yield _failed_sse(
                f"Direct-provider request failed ({type(exc).__name__}).",
                code="direct_provider_error",
            )

    async def _stream_anthropic(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        model = self.model_id()
        payload, catalog = responses_to_anthropic(body, model)
        lifecycle = ResponseEventStream(
            body, model, error_code="anthropic_provider_error"
        )
        for event in lifecycle.begin():
            yield event

        finished_items: dict[int, dict[str, Any]] = {}
        next_output_index = 0
        tool_states: dict[int, dict[str, Any]] = {}
        text = ""
        thinking = ""
        signature = ""
        text_id = f"msg_{os.urandom(16).hex()}"
        reasoning_id = f"rs_{os.urandom(16).hex()}"
        text_index: int | None = None
        reasoning_index: int | None = None
        text_started = False
        reasoning_started = False
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0

        async for event in self.client.stream_anthropic(payload):
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output_tokens") or 0)
            elif event_type == "content_block_start":
                index = event.get("index")
                block = event.get("content_block")
                if not isinstance(index, int) or not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    delta = block["text"]
                    if delta:
                        if not text_started:
                            text_index = next_output_index
                            next_output_index += 1
                            for chunk in lifecycle.start_message(
                                text_id, phase="final_answer", output_index=text_index
                            ):
                                yield chunk
                            text_started = True
                        text += delta
                        yield lifecycle.text_delta(
                            text_id, delta, output_index=text_index or 0
                        )
                elif kind == "thinking":
                    delta = block.get("thinking")
                    if isinstance(delta, str) and delta:
                        if not reasoning_started:
                            reasoning_index = next_output_index
                            next_output_index += 1
                            for chunk in lifecycle.start_reasoning(
                                reasoning_id, output_index=reasoning_index
                            ):
                                yield chunk
                            reasoning_started = True
                        thinking += delta
                        yield lifecycle.reasoning_delta(
                            reasoning_id, delta, output_index=reasoning_index or 0
                        )
                    if isinstance(block.get("signature"), str):
                        signature += block["signature"]
                elif kind == "redacted_thinking":
                    delta = "[Reasoning redacted by Anthropic]"
                    if not reasoning_started:
                        reasoning_index = next_output_index
                        next_output_index += 1
                        for chunk in lifecycle.start_reasoning(
                            reasoning_id, output_index=reasoning_index
                        ):
                            yield chunk
                        reasoning_started = True
                    thinking += delta
                    yield lifecycle.reasoning_delta(
                        reasoning_id, delta, output_index=reasoning_index or 0
                    )
                    if isinstance(block.get("data"), str):
                        signature += block["data"]
                elif kind == "tool_use":
                    tool_states[index] = {
                        "id": str(block.get("id") or f"call_{os.urandom(12).hex()}"),
                        "name": str(block.get("name") or ""),
                        "input": block.get("input")
                        if isinstance(block.get("input"), dict)
                        else {},
                        "partial": "",
                    }
            elif event_type == "content_block_delta":
                index = event.get("index")
                delta = event.get("delta")
                if not isinstance(index, int) or not isinstance(delta, dict):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                    value = delta["text"]
                    if not text_started:
                        text_index = next_output_index
                        next_output_index += 1
                        for chunk in lifecycle.start_message(
                            text_id, phase="final_answer", output_index=text_index
                        ):
                            yield chunk
                        text_started = True
                    text += value
                    yield lifecycle.text_delta(
                        text_id, value, output_index=text_index or 0
                    )
                elif delta_type == "thinking_delta" and isinstance(
                    delta.get("thinking"), str
                ):
                    value = delta["thinking"]
                    if not reasoning_started:
                        reasoning_index = next_output_index
                        next_output_index += 1
                        for chunk in lifecycle.start_reasoning(
                            reasoning_id, output_index=reasoning_index
                        ):
                            yield chunk
                        reasoning_started = True
                    thinking += value
                    yield lifecycle.reasoning_delta(
                        reasoning_id, value, output_index=reasoning_index or 0
                    )
                elif delta_type == "signature_delta" and isinstance(
                    delta.get("signature"), str
                ):
                    signature += delta["signature"]
                elif delta_type == "input_json_delta":
                    state = tool_states.get(index)
                    partial = delta.get("partial_json")
                    if state is not None and isinstance(partial, str):
                        state["partial"] += partial
            elif event_type == "content_block_stop":
                index = event.get("index")
                state = tool_states.get(index) if isinstance(index, int) else None
                if state is not None and state["partial"]:
                    try:
                        parsed = json.loads(state["partial"])
                    except json.JSONDecodeError as exc:
                        raise DirectAPIError(
                            "Anthropic returned incomplete tool arguments."
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise DirectAPIError(
                            "Anthropic returned non-object tool arguments."
                        )
                    state["input"] = parsed
            elif event_type == "message_delta":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("input_tokens") or input_tokens)
                    output_tokens = int(usage.get("output_tokens") or output_tokens)
                    details = usage.get("output_tokens_details")
                    if isinstance(details, dict):
                        reasoning_tokens = int(details.get("thinking_tokens") or 0)

        if reasoning_started:
            encrypted = encode_anthropic_signature(signature) if signature else None
            events, item = lifecycle.finish_reasoning(
                reasoning_id,
                thinking,
                output_index=reasoning_index or 0,
                encrypted_content=encrypted,
            )
            for event in events:
                yield event
            finished_items[reasoning_index or 0] = item
        if text_started:
            events, item = lifecycle.finish_message(
                text_id, text, phase="final_answer", output_index=text_index or 0
            )
            for event in events:
                yield event
            finished_items[text_index or 0] = item
        tool_items: list[dict[str, Any]] = []
        for index in sorted(tool_states):
            state = tool_states[index]
            item = tool_item(state["name"], state["input"], state["id"], catalog)
            if item is None:
                raise DirectAPIError(
                    f"Anthropic requested an unknown tool: {state['name'][:200]}"
                )
            tool_items.append(item)
        for event in lifecycle.completed_items(
            tool_items, start_index=next_output_index
        ):
            yield event
        for output_index, item in enumerate(tool_items, start=next_output_index):
            finished_items[output_index] = item
        items = [finished_items[index] for index in sorted(finished_items)]
        usage = responses_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        )
        self.last_usage = usage
        yield lifecycle.completed(items, usage)

    async def _stream_kiro(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        model = self.model_id()
        payload, catalog = responses_to_kiro(body, model)
        final_tool_name, current_message = _install_kiro_completion_protocol(
            payload, catalog
        )
        lifecycle = ResponseEventStream(body, model, error_code="kiro_direct_error")
        for event in lifecycle.begin():
            yield event

        text = ""
        thinking = ""
        text_id = f"msg_{os.urandom(16).hex()}"
        reasoning_id = f"rs_{os.urandom(16).hex()}"
        text_index: int | None = None
        reasoning_index: int | None = None
        text_started = False
        reasoning_started = False
        next_output_index = 0
        finished_items: dict[int, dict[str, Any]] = {}
        hidden_reasoning = bool(
            model.startswith("gpt-5.6") or re.search(r"claude-opus-4\.(?:7|8)", model)
        )
        hidden_open = False
        hidden_item: dict[str, Any] | None = None
        if hidden_reasoning:
            reasoning_index = next_output_index
            next_output_index += 1
            for event in lifecycle.start_reasoning(
                reasoning_id, output_index=reasoning_index
            ):
                yield event
            marker = "Reasoning hidden by Kiro"
            thinking = marker
            yield lifecycle.reasoning_delta(
                reasoning_id, marker, output_index=reasoning_index
            )
            reasoning_started = True
            hidden_open = True

        tags = _ThinkingTags()
        tool_states: list[dict[str, Any]] = []
        current_tool: dict[str, Any] | None = None
        input_tokens = 0
        output_tokens = 0

        async def close_hidden() -> AsyncIterator[bytes]:
            nonlocal hidden_open, hidden_item
            if hidden_open:
                hidden_open = False
                events, hidden_item = lifecycle.finish_reasoning(
                    reasoning_id,
                    thinking,
                    output_index=reasoning_index or 0,
                )
                for event in events:
                    yield event

        async def emit_tagged(kind: str, value: str) -> AsyncIterator[bytes]:
            nonlocal text, thinking, text_started, reasoning_started
            nonlocal text_index, reasoning_index, next_output_index
            if not value:
                return
            if kind == "thinking":
                if not reasoning_started:
                    reasoning_index = next_output_index
                    next_output_index += 1
                    for event in lifecycle.start_reasoning(
                        reasoning_id, output_index=reasoning_index
                    ):
                        yield event
                    reasoning_started = True
                thinking += value
                yield lifecycle.reasoning_delta(
                    reasoning_id, value, output_index=reasoning_index or 0
                )
            else:
                if not text_started:
                    text_index = next_output_index
                    next_output_index += 1
                    for event in lifecycle.start_message(
                        text_id, phase="commentary", output_index=text_index
                    ):
                        yield event
                    text_started = True
                text += value
                yield lifecycle.text_delta(text_id, value, output_index=text_index or 0)

        for attempt in range(_KIRO_MAX_ATTEMPTS):
            attempt_text_start = len(text)
            last_content = ""
            logger.info(
                "Kiro Direct agent attempt started attempt=%d max_attempts=%d",
                attempt + 1,
                _KIRO_MAX_ATTEMPTS,
            )
            async for event in self.client.stream_kiro(payload):
                if event.get("_switchboard_message_type") in {"error", "exception"}:
                    raise DirectAPIError("Kiro returned an event-stream exception.")
                if "content" in event and isinstance(event.get("content"), str):
                    value = event["content"]
                    if value == last_content:
                        continue
                    last_content = value
                    if hidden_open:
                        async for chunk in close_hidden():
                            yield chunk
                    if hidden_reasoning:
                        async for chunk in emit_tagged("text", value):
                            yield chunk
                    else:
                        for kind, delta in tags.feed(value):
                            async for chunk in emit_tagged(kind, delta):
                                yield chunk
                elif event.get("name") and event.get("toolUseId"):
                    if hidden_open:
                        async for chunk in close_hidden():
                            yield chunk
                    tool_id = str(event["toolUseId"])
                    if current_tool is None or current_tool["id"] != tool_id:
                        if current_tool is not None:
                            tool_states.append(current_tool)
                        current_tool = {
                            "id": tool_id,
                            "name": str(event["name"]),
                            "partial": "",
                        }
                    raw_input = event.get("input")
                    if isinstance(raw_input, str):
                        current_tool["partial"] += raw_input
                    elif isinstance(raw_input, dict) and raw_input:
                        current_tool["partial"] += json.dumps(
                            raw_input, ensure_ascii=False, separators=(",", ":")
                        )
                    if event.get("stop"):
                        tool_states.append(current_tool)
                        current_tool = None
                elif "input" in event and not event.get("name"):
                    if current_tool is not None:
                        raw_input = event.get("input")
                        current_tool["partial"] += (
                            raw_input
                            if isinstance(raw_input, str)
                            else json.dumps(raw_input, ensure_ascii=False)
                        )
                elif event.get("stop") is True and current_tool is not None:
                    tool_states.append(current_tool)
                    current_tool = None
                elif isinstance(event.get("contextUsagePercentage"), (int, float)):
                    input_tokens = round(
                        float(event["contextUsagePercentage"]) / 100 * 272_000
                    )
                elif isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                    input_tokens = int(usage.get("inputTokens") or input_tokens)
                    output_tokens += int(usage.get("outputTokens") or 0)
                elif event.get("error") is not None or event.get("Error") is not None:
                    raise DirectAPIError("Kiro returned a streaming error.")

            if current_tool is not None:
                tool_states.append(current_tool)
                current_tool = None
            if tool_states:
                logger.info(
                    "Kiro Direct agent attempt completed attempt=%d "
                    "terminal_action=tool_call tool_count=%d",
                    attempt + 1,
                    len(tool_states),
                )
                break
            if attempt + 1 >= _KIRO_MAX_ATTEMPTS:
                logger.warning(
                    "Kiro Direct agent attempt incomplete attempt=%d "
                    "terminal_action=missing text_chars=%d retry=false",
                    attempt + 1,
                    len(text) - attempt_text_start,
                )
                raise DirectAPIError(
                    "Kiro ended twice without requesting a tool or submitting a "
                    "final answer; refusing to report an incomplete response as "
                    "completed."
                )
            attempt_text = text[attempt_text_start:]
            logger.warning(
                "Kiro Direct agent attempt incomplete attempt=%d "
                "terminal_action=missing text_chars=%d retry=true",
                attempt + 1,
                len(attempt_text),
            )
            payload, current_message = _kiro_continuation_payload(
                payload, current_message, attempt_text
            )

        if not hidden_reasoning:
            for kind, delta in tags.feed("", final=True):
                async for chunk in emit_tagged(kind, delta):
                    yield chunk
        if hidden_open:
            async for chunk in close_hidden():
                yield chunk

        if reasoning_started:
            # Hidden reasoning was already closed before real output. For native
            # thinking tags, close it here.
            if not hidden_reasoning:
                events, item = lifecycle.finish_reasoning(
                    reasoning_id,
                    thinking,
                    output_index=reasoning_index or 0,
                )
                for event in events:
                    yield event
                finished_items[reasoning_index or 0] = item
            elif hidden_item is not None:
                finished_items[reasoning_index or 0] = hidden_item
        if text_started:
            events, item = lifecycle.finish_message(
                text_id, text, phase="commentary", output_index=text_index or 0
            )
            for event in events:
                yield event
            finished_items[text_index or 0] = item
        completed_tools: list[dict[str, Any]] = []
        final_answer: str | None = None
        for state in tool_states:
            raw = state["partial"].strip() or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DirectAPIError(
                    "Kiro returned incomplete tool arguments."
                ) from exc
            if not isinstance(arguments, dict):
                raise DirectAPIError("Kiro returned non-object tool arguments.")
            if state["name"] == final_tool_name:
                candidate = arguments.get("final_answer")
                if not isinstance(candidate, str) or not candidate.strip():
                    raise DirectAPIError("Kiro submitted an empty final answer.")
                final_answer = candidate
                continue
            item = tool_item(state["name"], arguments, state["id"], catalog)
            if item is None:
                raise DirectAPIError(
                    f"Kiro requested an unknown tool: {state['name'][:200]}"
                )
            completed_tools.append(item)
        if completed_tools:
            # A normal tool call means the agent still has work to do. Never let a
            # conflicting internal-final call suppress that tool round.
            final_answer = None
            logger.info(
                "Kiro Direct response completed terminal_action=client_tools "
                "tool_count=%d commentary_chars=%d",
                len(completed_tools),
                len(text),
            )
        elif final_answer is not None:
            logger.info(
                "Kiro Direct response completed terminal_action=final_answer "
                "final_chars=%d commentary_chars=%d",
                len(final_answer),
                len(text),
            )
            final_id = f"msg_{os.urandom(16).hex()}"
            final_index = next_output_index
            next_output_index += 1
            for event in lifecycle.start_message(
                final_id, phase="final_answer", output_index=final_index
            ):
                yield event
            yield lifecycle.text_delta(final_id, final_answer, output_index=final_index)
            events, item = lifecycle.finish_message(
                final_id,
                final_answer,
                phase="final_answer",
                output_index=final_index,
            )
            for event in events:
                yield event
            finished_items[final_index] = item
        for event in lifecycle.completed_items(
            completed_tools, start_index=next_output_index
        ):
            yield event
        for output_index, item in enumerate(completed_tools, start=next_output_index):
            finished_items[output_index] = item
        items = [finished_items[index] for index in sorted(finished_items)]
        if output_tokens <= 0:
            output_tokens = max(
                1,
                (len(text) + len(thinking) + len(final_answer or "")) // 4,
            )
        usage = responses_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=0,
        )
        self.last_usage = usage
        yield lifecycle.completed(items, usage)
