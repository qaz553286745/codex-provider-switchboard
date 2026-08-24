from __future__ import annotations

import json

import pytest

from codex_provider_switchboard.compatibility.profiles import (
    FUNCTION_ONLY,
    NATIVE_CODEX,
)
from codex_provider_switchboard.compatibility.responses import (
    ResponsesCompatibilityError,
    ResponsesStreamRestorer,
    adapt_responses_request,
    analyze_tool_continuation_coverage,
    bind_transport_context,
    collect_request_tools,
    forwarded_codex_headers,
    prepare_compaction_request,
    promote_additional_tools,
    restore_response_value,
)


def _tool_search_request() -> dict[str, object]:
    return {
        "tools": [{"type": "tool_search"}],
        "multi_agent": {"enabled": True, "max_concurrent_subagents": 2},
        "context_management": {"type": "compaction"},
        "input": [
            {
                "type": "tool_search_call",
                "id": "tsc_fixture",
                "call_id": "call_fixture",
                "arguments": {"query": "subagent"},
                "execution": "client",
                "status": "completed",
            },
            {
                "type": "tool_search_output",
                "id": "tso_fixture",
                "call_id": "call_fixture",
                "execution": "client",
                "status": "completed",
                "tools": [
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Run JavaScript.",
                    },
                    {
                        "type": "namespace",
                        "name": "multi_agent_v1",
                        "tools": [
                            {
                                "type": "function",
                                "name": "spawn_agent",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                    "required": ["message"],
                                },
                            },
                            {
                                "type": "function",
                                "name": "wait_agent",
                                "parameters": {"type": "object"},
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_dynamic_tool_search_is_available_to_prompt_bridges() -> None:
    tools = collect_request_tools(_tool_search_request())
    assert [(tool["type"], tool["name"]) for tool in tools] == [
        ("tool_search", "tool_search"),
        ("custom", "exec"),
        ("function", "multi_agent_v1.spawn_agent"),
        ("function", "multi_agent_v1.wait_agent"),
    ]
    assert tools[2]["_namespace"] == "multi_agent_v1"
    assert tools[2]["_wire_name"] == "spawn_agent"


def test_function_only_adapter_lowers_discovery_and_history_reversibly() -> None:
    adapted = adapt_responses_request(_tool_search_request(), FUNCTION_ONLY)
    assert [tool["name"] for tool in adapted.body["tools"]] == [
        "tool_search",
        "exec",
        "multi_agent_v1__spawn_agent",
        "multi_agent_v1__wait_agent",
    ]
    assert all(tool["type"] == "function" for tool in adapted.body["tools"])
    assert "multi_agent" not in adapted.body
    assert "context_management" not in adapted.body

    search_call, search_output = adapted.body["input"]
    assert search_call["type"] == "function_call"
    assert search_call["name"] == "tool_search"
    assert json.loads(search_call["arguments"]) == {"query": "subagent"}
    assert search_output["type"] == "function_call_output"
    discovered = json.loads(search_output["output"])
    assert discovered[1]["name"] == "multi_agent_v1"
    assert set(search_output) == {"type", "call_id", "output"}

    restored = restore_response_value(
        {
            "output": [
                {
                    "type": "function_call",
                    "name": "exec",
                    "call_id": "call_exec",
                    "arguments": '{"input":"pwd"}',
                },
                {
                    "type": "function_call",
                    "name": "tool_search",
                    "call_id": "call_search",
                    "arguments": '{"query":"github"}',
                },
                {
                    "type": "function_call",
                    "name": "multi_agent_v1__spawn_agent",
                    "call_id": "call_spawn",
                    "arguments": '{"message":"inspect"}',
                },
            ]
        },
        adapted.mapping,
    )
    assert restored["output"][0]["type"] == "custom_tool_call"
    assert restored["output"][0]["input"] == "pwd"
    assert restored["output"][1]["type"] == "tool_search_call"
    assert restored["output"][1]["arguments"] == {"query": "github"}
    assert restored["output"][2]["namespace"] == "multi_agent_v1"
    assert restored["output"][2]["name"] == "spawn_agent"


def test_long_namespace_tool_name_has_bounded_stable_alias() -> None:
    namespace = "connector_namespace_" * 4
    name = "lookup_repository_metadata_" * 3
    body = {
        "tools": [
            {
                "type": "namespace",
                "name": namespace,
                "tools": [
                    {
                        "type": "function",
                        "name": name,
                        "parameters": {"type": "object"},
                    }
                ],
            }
        ]
    }

    first = adapt_responses_request(body, FUNCTION_ONLY)
    second = adapt_responses_request(body, FUNCTION_ONLY)
    alias = first.body["tools"][0]["name"]

    assert alias == second.body["tools"][0]["name"]
    assert len(alias) == 64
    restored = restore_response_value(
        {"output": [{"type": "function_call", "name": alias}]}, first.mapping
    )
    assert restored["output"][0]["name"] == name
    assert restored["output"][0]["namespace"] == namespace


def test_stream_restorer_buffers_client_tool_arguments_and_resequences() -> None:
    mapping = adapt_responses_request(_tool_search_request(), FUNCTION_ONLY).mapping
    restorer = ResponsesStreamRestorer(mapping)
    added = restorer.restore(
        {
            "type": "response.output_item.added",
            "sequence_number": 4,
            "output_index": 0,
            "agent": {"id": "root"},
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "exec",
                "arguments": "",
            },
        }
    )
    assert added[0]["item"]["type"] == "custom_tool_call"
    assert added[0]["agent"] == {"id": "root"}
    assert added[0]["sequence_number"] == 4
    assert (
        restorer.restore(
            {
                "type": "response.function_call_arguments.delta",
                "sequence_number": 5,
                "output_index": 0,
                "item_id": "fc_1",
                "delta": '{"input":"pw',
            }
        )
        == []
    )
    done = restorer.restore(
        {
            "type": "response.function_call_arguments.done",
            "sequence_number": 6,
            "output_index": 0,
            "item_id": "fc_1",
            "call_id": "call_1",
            "name": "exec",
            "arguments": '{"input":"pwd"}',
        }
    )
    assert [event["type"] for event in done] == [
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
    ]
    assert [event["sequence_number"] for event in done] == [5, 6]
    assert done[-1]["input"] == "pwd"


def test_native_profile_preserves_multi_agent_namespace_and_tool_search() -> None:
    body = _tool_search_request()
    adapted = adapt_responses_request(body, NATIVE_CODEX)
    assert adapted.body == body
    assert adapted.mapping.empty
    assert adapted.body is not body


def test_native_additional_tool_promotion_preserves_builtins_and_infers_custom() -> (
    None
):
    promoted = promote_additional_tools(
        {
            "tools": [{"type": "web_search"}, {"type": "tool_search"}],
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [{"name": "exec", "description": "Run JavaScript."}],
                },
                {"role": "user", "content": "inspect"},
            ],
        }
    )
    assert promoted["tools"] == [
        {"type": "web_search"},
        {"type": "tool_search"},
        {"type": "custom", "name": "exec", "description": "Run JavaScript."},
    ]
    assert promoted["input"] == [{"role": "user", "content": "inspect"}]


def test_transport_context_forwards_only_allowlisted_headers() -> None:
    body = bind_transport_context(
        {"input": "hello", "client_metadata": {"thread_id": "child-1"}},
        {
            "openai-beta": "responses_multi_agent=v1",
            "x-openai-subagent": "worker",
            "x-codex-parent-thread-id": "parent-1",
            "authorization": "Bearer must-not-forward",
        },
    )
    assert body["client_metadata"]["thread_id"] == "child-1"
    assert forwarded_codex_headers(body) == {
        "OpenAI-Beta": "responses_multi_agent=v1",
        "x-openai-subagent": "worker",
        "x-codex-parent-thread-id": "parent-1",
    }


def test_tool_continuation_coverage_requires_every_call_context() -> None:
    covered = analyze_tool_continuation_coverage(
        {
            "input": [
                {"type": "item_reference", "id": "call_a"},
                {
                    "type": "mcp_tool_call",
                    "call_id": "call_b",
                    "name": "lookup",
                },
                {"type": "tool_search_output", "call_id": "call_a", "tools": []},
                {"type": "mcp_tool_call_output", "call_id": "call_b", "output": "ok"},
            ]
        }
    )
    assert covered.has_outputs is True
    assert covered.context_covers_all_call_ids is True

    missing = analyze_tool_continuation_coverage(
        {"input": [{"type": "function_call_output", "call_id": "orphan"}]}
    )
    assert missing.context_covers_all_call_ids is False


def test_compaction_request_is_narrow_and_multi_agent_uses_automatic_path() -> None:
    compact = prepare_compaction_request(
        {
            "instructions": "Continue.",
            "tools": [{"type": "function", "name": "read"}],
            "input": [
                {"type": "additional_tools", "tools": []},
                {
                    "type": "function_call",
                    "namespace": "files",
                    "name": "read",
                    "call_id": "call_1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": {"namespace": "business-data", "value": "ok"},
                },
            ],
        },
        model="gpt-test",
    )
    assert compact == {
        "model": "gpt-test",
        "instructions": "Continue.",
        "input": [
            {
                "type": "function_call",
                "name": "read",
                "call_id": "call_1",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": {"namespace": "business-data", "value": "ok"},
            },
        ],
    }
    previous = prepare_compaction_request(
        {
            "previous_response_id": "resp_previous",
            "prompt_cache_key": "task-cache",
            "service_tier": "default",
            "tools": [{"type": "function", "name": "ignored"}],
        },
        model="gpt-test",
    )
    assert previous == {
        "model": "gpt-test",
        "previous_response_id": "resp_previous",
        "prompt_cache_key": "task-cache",
        "service_tier": "default",
    }
    with pytest.raises(ResponsesCompatibilityError, match="automatically"):
        prepare_compaction_request(
            {"input": [], "multi_agent": {"enabled": True}}, model="gpt-test"
        )


def test_conflicting_discovered_tool_schema_is_rejected() -> None:
    body = _tool_search_request()
    body["tools"] = [
        {"type": "tool_search"},
        {
            "type": "custom",
            "name": "exec",
            "description": "A different declaration.",
        },
    ]
    with pytest.raises(ResponsesCompatibilityError, match="Conflicting"):
        collect_request_tools(body)
