from __future__ import annotations

import json
import re

from codex_provider_switchboard.domain.direct_bridge import (
    analyze_tool_continuation,
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


def _running_exec_body() -> dict:
    return {
        "instructions": "Continue the task.",
        "tools": [
            {
                "type": "custom",
                "name": "exec",
                "description": "Run JavaScript that can call nested tools.",
            }
        ],
        "input": [
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-run",
                "input": (
                    'const r = await tools.exec_command({cmd: "pytest -q"}); '
                    "text(JSON.stringify(r));"
                ),
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-run",
                "output": [
                    {"type": "input_text", "text": "Script completed\n"},
                    {
                        "type": "input_text",
                        "text": (
                            '{"session_id":75696,"output":"... [45%]",'
                            '"wall_time_seconds":30.0}'
                        ),
                    },
                ],
            },
        ],
    }


def test_running_exec_session_is_detected_and_explained_to_direct_models() -> None:
    body = _running_exec_body()
    state = analyze_tool_continuation(body)
    assert state.pending_session_ids == (75696,)
    assert not state.lost_session_handle

    kiro, _ = responses_to_kiro(body, "gpt-5.6-sol")
    current = kiro["conversationState"]["currentMessage"]["userInputMessage"]
    assert "detected running Codex exec session(s): 75696" in current["content"]
    exec_spec = current["userInputMessageContext"]["tools"][0]["toolSpecification"]
    assert "text(JSON.stringify(result))" in exec_spec["description"]
    assert "tools.write_stdin" in exec_spec["description"]
    assert "Do not launch the same command again" in exec_spec["description"]

    anthropic, _ = responses_to_anthropic(body, "claude-test")
    assert "tools.write_stdin" in anthropic["system"]
    assert "tools.write_stdin" in anthropic["tools"][0]["description"]


def test_completed_write_stdin_result_clears_pending_exec_session() -> None:
    body = _running_exec_body()
    body["input"].extend(
        [
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-poll",
                "input": (
                    "const r = await tools.write_stdin({session_id: 75696, "
                    'chars: ""}); text(JSON.stringify(r));'
                ),
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-poll",
                "output": '{"exit_code":0,"output":"158 passed"}',
            },
        ]
    )

    state = analyze_tool_continuation(body)
    assert state.pending_session_ids == ()


def test_discarded_exec_handle_is_detected_without_inventing_a_session() -> None:
    body = _running_exec_body()
    body["input"][1]["output"] = "... [45%]\nexit_code=undefined"

    state = analyze_tool_continuation(body)
    assert state.pending_session_ids == ()
    assert state.lost_session_handle

    kiro, _ = responses_to_kiro(body, "gpt-5.6-sol")
    content = kiro["conversationState"]["currentMessage"]["userInputMessage"]["content"]
    assert "potentially still running" in content
    assert "never start multiple identical replacements" in content


def test_direct_payloads_include_safe_subagent_lifecycle_guidance() -> None:
    body = {
        "instructions": "Coordinate the requested review.",
        "tools": [
            {
                "type": "namespace",
                "name": "collaboration",
                "tools": [
                    {
                        "type": "function",
                        "name": "interrupt_agent",
                        "parameters": {"type": "object"},
                    },
                    {
                        "type": "function",
                        "name": "followup_task",
                        "parameters": {"type": "object"},
                    },
                ],
            }
        ],
        "input": "Continue the review.",
    }

    anthropic, _ = responses_to_anthropic(body, "claude-test")
    kiro, _ = responses_to_kiro(body, "gpt-5.6-sol")
    kiro_content = kiro["conversationState"]["currentMessage"]["userInputMessage"][
        "content"
    ]

    assert "Subagent orchestration safety" in anthropic["system"]
    assert "Never alternate ``interrupt_agent``" in anthropic["system"]
    assert "Subagent orchestration safety" in kiro_content
    assert "Never alternate ``interrupt_agent``" in kiro_content


def test_codex_new_task_agent_message_becomes_the_active_kiro_task() -> None:
    body = {
        "instructions": "Work carefully.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Continue the old refactor."}
                ],
            },
            {
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/outcome_boundary",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Message Type: NEW_TASK\nPayload:\n",
                    },
                    {
                        "type": "encrypted_content",
                        "encrypted_content": (
                            "Read-only: inspect the failed terminal outcome."
                        ),
                    },
                ],
            },
            {
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "opaque-reasoning-must-not-leak",
            },
        ],
    }

    messages = responses_conversation(body)
    assert messages[-1].kind == "agent_new_task"
    assert "Read-only: inspect the failed terminal outcome." in str(
        messages[-1].content
    )

    kiro, _ = responses_to_kiro(body, "gpt-5.6-sol")
    state = kiro["conversationState"]
    current = state["currentMessage"]["userInputMessage"]["content"]
    history_text = json.dumps(state.get("history", []), ensure_ascii=False)
    assert "ACTIVE CODEX SUBAGENT TASK" in current
    assert "Read-only: inspect the failed terminal outcome." in current
    assert "Continue the old refactor." not in current
    assert "Continue the old refactor." in history_text
    assert "opaque-reasoning-must-not-leak" not in json.dumps(kiro, ensure_ascii=False)

    anthropic, _ = responses_to_anthropic(body, "claude-test")
    latest = anthropic["messages"][-1]["content"][0]["text"]
    assert "ACTIVE CODEX SUBAGENT TASK" in latest
    assert "Read-only: inspect the failed terminal outcome." in latest


def test_codex_final_answer_agent_message_is_preserved_for_the_parent() -> None:
    body = {
        "input": [
            {
                "type": "agent_message",
                "author": "/root/reviewer",
                "recipient": "/root",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Message Type: FINAL_ANSWER\nPayload:\n",
                    },
                    {
                        "type": "encrypted_content",
                        "encrypted_content": "The review found one blocking defect.",
                    },
                ],
            }
        ]
    }

    kiro, _ = responses_to_kiro(body, "gpt-5.6-sol")
    current = kiro["conversationState"]["currentMessage"]["userInputMessage"]["content"]
    assert "CODEX SUBAGENT RESULT" in current
    assert "The review found one blocking defect." in current


def test_opaque_agent_message_content_is_not_interpreted_without_legacy_marker() -> (
    None
):
    messages = responses_conversation(
        {
            "input": [
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/worker",
                    "content": [
                        {
                            "type": "encrypted_content",
                            "encrypted_content": "opaque-secret-agent-payload",
                        }
                    ],
                }
            ]
        }
    )
    assert messages == []


def test_kiro_direct_receives_discovered_tool_search_and_namespace_tools() -> None:
    body = {
        "tools": [{"type": "tool_search"}],
        "input": [
            {
                "type": "tool_search_output",
                "call_id": "search-1",
                "status": "completed",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "multi_agent_v1",
                        "tools": [
                            {
                                "type": "function",
                                "name": "spawn_agent",
                                "parameters": {"type": "object"},
                            }
                        ],
                    }
                ],
            },
            {"type": "message", "role": "user", "content": "Delegate this."},
        ],
    }

    payload, catalog = responses_to_kiro(body, "gpt-test")
    search_alias = next(
        alias for alias, tool in catalog.items() if tool["type"] == "tool_search"
    )
    spawn_alias = next(
        alias
        for alias, tool in catalog.items()
        if tool.get("_wire_name") == "spawn_agent"
    )
    specifications = payload["conversationState"]["currentMessage"]["userInputMessage"][
        "userInputMessageContext"
    ]["tools"]
    names = {item["toolSpecification"]["name"] for item in specifications}
    assert {search_alias, spawn_alias} <= names

    search = tool_item(search_alias, {"query": "github"}, "call-s", catalog)
    assert search is not None
    assert search["type"] == "tool_search_call"
    assert search["execution"] == "client"

    spawn = tool_item(spawn_alias, {"message": "inspect"}, "call-a", catalog)
    assert spawn is not None
    assert spawn["type"] == "function_call"
    assert spawn["namespace"] == "multi_agent_v1"
    assert spawn["name"] == "spawn_agent"


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
