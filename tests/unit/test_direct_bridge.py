from __future__ import annotations

import json
import re

from codex_provider_switchboard.domain.direct_bridge import (
    encode_anthropic_signature,
    responses_conversation,
    responses_to_anthropic,
    responses_to_kiro,
    tool_item,
)


def _request() -> dict:
    return {
        "instructions": "Work as an agent.",
        "reasoning": {"effort": "max", "summary": "auto"},
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read one file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Inspect it"}],
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Checking"}],
                "encrypted_content": encode_anthropic_signature("sig-1"),
            },
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "call-1",
                "arguments": '{"path":"README.md"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "contents",
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue"}],
            },
        ],
    }


def test_responses_are_translated_without_losing_tools_or_reasoning() -> None:
    body = _request()
    messages = responses_conversation(body)
    assert any(
        block.get("signature") == "sig-1"
        for message in messages
        for block in message.content
    )

    anthropic, catalog = responses_to_anthropic(body, "claude-sonnet-4-6")
    assert anthropic["system"] == "Work as an agent."
    assert anthropic["thinking"]["type"] == "adaptive"
    assert anthropic["tools"][0]["name"] == "read_file"
    assert any(
        block.get("type") == "tool_result"
        for message in anthropic["messages"]
        for block in message["content"]
    )

    kiro, kiro_catalog = responses_to_kiro(body, "gpt-5.6-sol")
    conversation = kiro["conversationState"]
    assert conversation["agentTaskType"] == "vibe"
    current = conversation["currentMessage"]["userInputMessage"]
    assert current["modelId"] == "gpt-5.6-sol"
    assert (
        current["userInputMessageContext"]["tools"][0]["toolSpecification"]["name"]
        == "read_file"
    )
    assert catalog.keys() == kiro_catalog.keys()


def test_tool_calls_restore_wire_name_only_after_complete_arguments() -> None:
    catalog = {
        "workspace.read": {
            "type": "function",
            "name": "workspace.read",
            "_wire_name": "read",
            "_namespace": "workspace",
        }
    }
    item = tool_item("workspace.read", {"path": "a.txt"}, "call-1", catalog)
    assert item is not None
    assert item["name"] == "read"
    assert item["namespace"] == "workspace"
    assert json.loads(item["arguments"]) == {"path": "a.txt"}


def test_kiro_namespace_tools_use_safe_stable_aliases_and_restore_codex_names() -> None:
    long_namespace = "namespace_" + "n" * 100
    body = {
        "tools": [
            {
                "type": "namespace",
                "name": "alpha",
                "tools": [
                    {
                        "type": "function",
                        "name": "shared",
                        "description": "Alpha shared tool",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
            {
                "type": "namespace",
                "name": "beta",
                "tools": [
                    {
                        "type": "function",
                        "name": "shared",
                        "description": "Beta shared tool",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
            {
                "type": "namespace",
                "name": long_namespace,
                "tools": [
                    {
                        "type": "function",
                        "name": "tool_" + "x" * 100,
                        "description": "Long namespace tool",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
            {
                "type": "function",
                "name": "plain_tool",
                "description": "Plain function",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "type": "custom",
                "name": "exec",
                "description": "Custom executor",
            },
        ],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Use alpha"}],
            },
            {
                "type": "function_call",
                "name": "shared",
                "namespace": "alpha",
                "call_id": "call-alpha",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-alpha",
                "output": "done",
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue"}],
            },
        ],
    }

    payload, catalog = responses_to_kiro(body, "gpt-5.6-sol")
    _, repeated_catalog = responses_to_kiro(body, "gpt-5.6-sol")
    aliases_by_namespace = {
        tool["_namespace"]: alias
        for alias, tool in catalog.items()
        if isinstance(tool.get("_namespace"), str)
    }
    alpha_alias = aliases_by_namespace["alpha"]
    beta_alias = aliases_by_namespace["beta"]

    assert tuple(catalog) == tuple(repeated_catalog)
    assert alpha_alias != beta_alias
    assert "." not in alpha_alias
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) for alias in catalog)
    assert {"plain_tool", "exec"}.issubset(catalog)

    collision_body = {
        **body,
        "tools": [
            *body["tools"],
            {
                "type": "function",
                "name": alpha_alias,
                "description": "Plain tool colliding with a generated alias",
                "parameters": {"type": "object", "properties": {}},
            },
        ],
    }
    _, collision_catalog = responses_to_kiro(collision_body, "gpt-5.6-sol")
    collision_aliases_by_namespace = {
        tool["_namespace"]: alias
        for alias, tool in collision_catalog.items()
        if isinstance(tool.get("_namespace"), str)
    }
    assert alpha_alias in collision_catalog
    assert collision_catalog[alpha_alias].get("_namespace") is None
    assert collision_aliases_by_namespace["alpha"] != alpha_alias
    assert all(
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) for alias in collision_catalog
    )

    conversation = payload["conversationState"]
    specs = conversation["currentMessage"]["userInputMessage"][
        "userInputMessageContext"
    ]["tools"]
    assert {item["toolSpecification"]["name"] for item in specs} == set(catalog)
    historical_tool_uses = [
        tool_use
        for message in conversation["history"]
        for tool_use in message.get("assistantResponseMessage", {}).get("toolUses", [])
    ]
    assert historical_tool_uses[0]["name"] == alpha_alias

    restored = tool_item(alpha_alias, {}, "call-new", catalog)
    assert restored is not None
    assert restored["name"] == "shared"
    assert restored["namespace"] == "alpha"
