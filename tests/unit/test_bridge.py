import json

import pytest

from codex_provider_switchboard.domain.bridge import (
    BridgePromptTooLargeError,
    BridgeProtocolError,
    BridgeResult,
    BridgeUpstreamContextOverflowError,
    BridgeUpstreamOutputTruncatedError,
    BridgeUpstreamRetryableError,
    StreamingMessageParser,
    clean_kiro_stdout,
    codex_thread_key,
    codex_thread_key_hash,
    collect_request_tools,
    kiro_effort_from_body,
    output_items,
    parse_bridge_output,
    render_bridge_prompt,
    request_summary,
    streaming_events,
)


def _request_payload(prompt: str) -> dict:
    encoded = prompt.split("RESPONSE_REQUEST_JSON\n", 1)[1].split(
        "\nEND_RESPONSE_REQUEST_JSON", 1
    )[0]
    return json.loads(encoded)


def test_clean_kiro_stdout() -> None:
    value = "\x1b[m> \x1b[0mKIRO_OK\n\n ▸ Credits: 0.06 • Time: 3s\n"
    assert clean_kiro_stdout(value) == "KIRO_OK"


def test_parse_message_envelope() -> None:
    nonce = "abc123"
    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_abc123\n"
        '{"kind":"message","text":"hello"}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_abc123"
    )
    assert parse_bridge_output(value, [], nonce) == BridgeResult(text="hello")


def test_parse_function_and_custom_tools() -> None:
    nonce = "n"
    tools = [
        {"type": "function", "name": "weather", "parameters": {}},
        {"type": "custom", "name": "shell"},
    ]
    envelope = {
        "kind": "tool_calls",
        "calls": [
            {"name": "weather", "payload": {"city": "Shanghai"}},
            {"name": "shell", "payload": "pwd"},
        ],
    }
    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps(envelope)
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    result = parse_bridge_output(value, tools, nonce)
    assert result.text is None
    assert result.tool_calls[0].payload == '{"city":"Shanghai"}'
    assert result.tool_calls[1].payload == "pwd"


def test_tool_search_bridge_call_round_trips_as_client_execution() -> None:
    tools = collect_request_tools({"tools": [{"type": "tool_search"}]})
    envelope = {
        "kind": "tool_calls",
        "calls": [{"name": "tool_search", "payload": {"query": "github"}}],
    }
    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_search\n"
        + json.dumps(envelope)
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_search"
    )

    result = parse_bridge_output(value, tools, "search")
    items = output_items(result)

    assert result.tool_calls[0].tool_type == "tool_search"
    assert items[0]["type"] == "tool_search_call"
    assert items[0]["arguments"] == {"query": "github"}
    assert items[0]["execution"] == "client"


def test_parse_tool_calls_with_user_visible_commentary() -> None:
    envelope = {
        "kind": "tool_calls",
        "commentary": "I will verify the configuration, then run validation.",
        "calls": [{"name": "weather", "payload": {"city": "Shanghai"}}],
    }
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps(envelope, ensure_ascii=False)
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )

    result = parse_bridge_output(
        wire,
        [{"type": "function", "name": "weather", "parameters": {}}],
        "n",
    )

    assert result.text is None
    assert result.commentary == "I will verify the configuration, then run validation."
    assert result.tool_calls[0].name == "weather"


def test_output_items_preserve_commentary_and_final_answer_phases() -> None:
    final_items = output_items(BridgeResult(text="完成。"))
    assert final_items[0]["phase"] == "final_answer"

    result = BridgeResult(
        text=None,
        commentary="正在检查。",
        tool_calls=(
            parse_bridge_output(
                (
                    "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
                    '{"kind":"tool_calls","calls":'
                    '[{"name":"shell","payload":"pwd"}]}\n'
                    "CODEX_SWITCHBOARD_BRIDGE_END_n"
                ),
                [{"type": "custom", "name": "shell"}],
                "n",
            ).tool_calls[0],
        ),
    )
    items = output_items(result)

    assert [item["type"] for item in items] == ["message", "custom_tool_call"]
    assert items[0]["phase"] == "commentary"
    assert items[0]["content"][0]["text"] == "正在检查。"


def test_commentary_can_precede_real_plan_and_work_tools() -> None:
    envelope = {
        "kind": "tool_calls",
        "commentary": "I will set the plan, then inspect the repository.",
        "calls": [
            {
                "name": "update_plan",
                "payload": {
                    "plan": [
                        {"step": "Inspect the repository", "status": "in_progress"},
                        {"step": "Apply the fix", "status": "pending"},
                        {"step": "Run validation", "status": "pending"},
                    ]
                },
            },
            {"name": "exec", "payload": "const r = await inspect(); text(r);"},
        ],
    }
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps(envelope)
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )

    result = parse_bridge_output(
        wire,
        [
            {"type": "function", "name": "update_plan", "parameters": {}},
            {"type": "custom", "name": "exec"},
        ],
        "n",
    )
    items = output_items(result)

    assert [item["type"] for item in items] == [
        "message",
        "function_call",
        "custom_tool_call",
    ]
    assert items[0]["phase"] == "commentary"
    assert items[1]["name"] == "update_plan"
    assert items[2]["name"] == "exec"


def test_plain_text_fallback() -> None:
    assert parse_bridge_output("plain answer", [], "n") == BridgeResult(
        text="plain answer"
    )


def test_upstream_context_status_is_not_accepted_as_an_answer() -> None:
    with pytest.raises(BridgeUpstreamContextOverflowError, match="context-window"):
        parse_bridge_output(
            "The context window has overflowed, summarizing the history...",
            [],
            "n",
        )


def test_mixed_context_and_truncation_status_is_not_accepted_as_an_answer() -> None:
    value = (
        "The context window has overflowed, summarizing the history...\n\n"
        "> CODEX_SWITCHBOARD_B...content truncated due to length"
    )
    with pytest.raises(BridgeUpstreamContextOverflowError, match="context-window"):
        parse_bridge_output(value, [], "n")


def test_truncated_bridge_prefix_is_not_accepted_as_an_answer() -> None:
    with pytest.raises(BridgeUpstreamOutputTruncatedError, match="truncated"):
        parse_bridge_output(
            "CODEX_SWITCHBOARD_B...content truncated due to length", [], "n"
        )


def test_retryable_status_inside_current_envelope_is_rejected() -> None:
    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps(
            {
                "kind": "message",
                "text": (
                    "The context window has overflowed, summarizing the history..."
                ),
            }
        )
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    with pytest.raises(BridgeUpstreamContextOverflowError, match="context-window"):
        parse_bridge_output(value, [], "n")


def test_request_summary_distinguishes_top_level_and_effective_tools() -> None:
    summary = request_summary(
        {
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [{"name": "exec", "description": "FREEFORM"}],
                },
                {"type": "message", "role": "user", "content": []},
            ],
            "tools": [],
        }
    )
    assert summary["top_level_tool_count"] == 0
    assert summary["effective_tool_count"] == 1
    assert summary["collected_tools"] == [{"name": "exec", "type": "custom"}]


def test_stale_or_nested_bridge_envelopes_are_never_plain_text() -> None:
    stale = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_old\n"
        '{"kind":"message","text":"stale"}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_old"
    )
    with pytest.raises(BridgeProtocolError, match="Stale"):
        parse_bridge_output(stale, [], "current")

    nested = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_current\n"
        + json.dumps({"kind": "message", "text": stale})
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_current"
    )
    with pytest.raises(BridgeProtocolError, match="Nested"):
        parse_bridge_output(nested, [], "current")


def test_malformed_current_envelope_is_not_returned_as_text() -> None:
    malformed = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_current\n"
        '{"kind":"tool_calls","calls":['
        "\nCODEX_SWITCHBOARD_BRIDGE_END_current"
    )
    with pytest.raises(BridgeProtocolError, match="invalid"):
        parse_bridge_output(malformed, [], "current")


def test_implicit_codex_exec_is_custom_when_tools_are_omitted() -> None:
    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        '{"kind":"tool_calls","calls":[{"name":"exec","payload":"text(true);"}]}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    tools = collect_request_tools(
        {"instructions": "The exec tool has a FREEFORM input grammar."}
    )
    result = parse_bridge_output(value, tools, "n")
    assert result.text is None
    assert result.tool_calls[0].tool_type == "custom"
    assert result.tool_calls[0].payload == "text(true);"


def test_unknown_or_non_object_function_calls_are_rejected() -> None:
    unknown = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        '{"kind":"tool_calls","calls":[{"name":"unknown","payload":{}}]}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    result = parse_bridge_output(unknown, [{"type": "function", "name": "known"}], "n")
    assert "Rejected unknown tool name" in (result.text or "")

    scalar = unknown.replace('"unknown","payload":{}', '"known","payload":[]')
    result = parse_bridge_output(scalar, [{"type": "function", "name": "known"}], "n")
    assert "must be an object" in (result.text or "")


def test_collect_codex_additional_tools() -> None:
    body = {
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"name": "exec", "description": "FREEFORM"}],
            },
        ]
    }
    assert collect_request_tools(body) == [
        {"name": "exec", "description": "FREEFORM", "type": "custom"}
    ]


def test_collect_namespaced_function_tools() -> None:
    body = {
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "collaboration",
                        "tools": [
                            {
                                "type": "function",
                                "name": "list_agents",
                                "parameters": {"type": "object"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    tools = collect_request_tools(body)
    assert tools[0]["name"] == "collaboration.list_agents"
    assert tools[0]["_wire_name"] == "list_agents"
    assert tools[0]["_namespace"] == "collaboration"

    value = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        '{"kind":"tool_calls","calls":['
        '{"name":"collaboration.list_agents","payload":{}}]}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    result = parse_bridge_output(value, tools, "n")
    assert result.tool_calls[0].name == "list_agents"
    assert result.tool_calls[0].namespace == "collaboration"


def test_streaming_text_lifecycle() -> None:
    body = {"model": "gpt-5.6-sol", "input": "hi", "stream": True}
    events, completed = streaming_events(
        body, "gpt-5.6-sol", BridgeResult(text="hello"), "prompt"
    )
    types = [event["type"] for event in events]
    assert types[0] == "response.created"
    assert "response.output_text.delta" in types
    assert types[-1] == "response.completed"
    assert completed["output"][0]["content"][0]["text"] == "hello"
    assert completed["output"][0]["phase"] == "final_answer"


def test_render_prompt_contains_nonce_and_request() -> None:
    prompt = render_bridge_prompt(
        {
            "input": "你好",
            "tools": [],
            "reasoning": {"effort": "max"},
        },
        "nonce",
    )
    assert "CODEX_SWITCHBOARD_BRIDGE_BEGIN_nonce" in prompt
    assert "你好" in prompt
    assert '"reasoning":{"effort":"max"}' in prompt
    assert "tool-call JSON envelope is inert protocol text" in prompt
    assert "Never tell the user to" in prompt
    assert "between Ask and Agent modes" in prompt
    assert '"commentary":"brief user-visible progress update"' in prompt
    assert "not hidden" in prompt
    assert "update_plan" in prompt
    assert "through the nested tools exposed by ``exec``" in prompt


def test_render_continuation_prompt_marks_delta_input() -> None:
    prompt = render_bridge_prompt(
        {"input": [{"type": "message", "role": "user"}]},
        "nonce",
        continuation=True,
    )
    assert "contains only the new items" in prompt
    assert "do not repeat your previous response" in prompt


def test_render_prompt_batches_exec_work_when_top_level_parallelism_is_off() -> None:
    prompt = render_bridge_prompt(
        {
            "input": "inspect the repository",
            "parallel_tool_calls": False,
            "tools": [{"type": "custom", "name": "exec"}],
        },
        "nonce",
    )

    assert "preferred batching boundary" in prompt
    assert "await Promise.all([...])" in prompt
    assert "at most one top-level tool call per envelope" in prompt
    assert "One exec call may still coordinate independent" in prompt
    payload = json.loads(
        prompt.split("RESPONSE_REQUEST_JSON\n", 1)[1].split(
            "\nEND_RESPONSE_REQUEST_JSON", 1
        )[0]
    )
    assert payload["parallel_tool_calls"] is False


def test_render_prompt_groups_parallel_top_level_calls_without_exec() -> None:
    prompt = render_bridge_prompt(
        {
            "input": "check two services",
            "parallel_tool_calls": True,
            "tools": [
                {"type": "function", "name": "first", "parameters": {}},
                {"type": "function", "name": "second", "parameters": {}},
            ],
        },
        "nonce",
    )

    assert "all independent top-level tool calls together" in prompt
    assert "await Promise.all([...])" not in prompt
    payload = json.loads(
        prompt.split("RESPONSE_REQUEST_JSON\n", 1)[1].split(
            "\nEND_RESPONSE_REQUEST_JSON", 1
        )[0]
    )
    assert payload["parallel_tool_calls"] is True


def test_render_prompt_explains_safe_subagent_lifecycle() -> None:
    prompt = render_bridge_prompt(
        {
            "input": "Coordinate the requested review.",
            "tools": [
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "tools": [
                        {"type": "function", "name": "interrupt_agent"},
                        {"type": "function", "name": "followup_task"},
                        {"type": "function", "name": "wait_agent"},
                    ],
                }
            ],
        },
        "nonce",
    )

    assert "Subagent orchestration safety" in prompt
    assert "Never alternate ``interrupt_agent`` and ``followup_task``" in prompt
    assert "use it for waiting" in prompt


def test_render_prompt_compacts_metadata_and_trims_oldest_complete_turns() -> None:
    old_marker = "OLD_HISTORY_" + ("x" * 12_000)
    latest_marker = "LATEST_USER_TURN"
    additional_tool = {
        "type": "additional_tools",
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {"type": "object"},
            }
        ],
    }
    body = {
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": "PRESERVED_DEVELOPER_PREFIX",
            },
            {
                "id": "msg_old",
                "status": "completed",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": old_marker}],
            },
            {
                "id": "msg_answer",
                "status": "completed",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "old answer",
                        "annotations": [{"secret": "transport-only"}],
                        "logprobs": [{"token": "unused"}],
                    }
                ],
            },
            {
                "type": "reasoning",
                "encrypted_content": "opaque-reasoning-transport-data",
            },
            additional_tool,
            {
                "id": "msg_latest",
                "status": "completed",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": latest_marker}],
            },
        ]
    }
    latest_only_size = len(
        render_bridge_prompt(
            {
                **body,
                "input": [body["input"][0], additional_tool, body["input"][-1]],
            },
            "nonce",
        ).encode()
    )

    prompt = render_bridge_prompt(
        body,
        "nonce",
        max_bytes=latest_only_size + 1_024,
    )
    payload = _request_payload(prompt)

    assert len(prompt.encode()) <= latest_only_size + 1_024
    assert old_marker not in prompt
    assert latest_marker in prompt
    assert "PRESERVED_DEVELOPER_PREFIX" in prompt
    assert payload["history_truncation"]["strategy"] == ("oldest_complete_user_turns")
    assert payload["tools"][0]["name"] == "lookup"
    assert all(item.get("type") != "additional_tools" for item in payload["input"])
    assert "opaque-reasoning-transport-data" not in prompt
    assert "transport-only" not in prompt


def test_render_prompt_rejects_an_untrimmable_active_turn() -> None:
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "x" * 10_000}],
            }
        ]
    }
    with pytest.raises(BridgePromptTooLargeError, match="Start a new task"):
        render_bridge_prompt(body, "nonce", max_bytes=4_096)


def test_render_continuation_never_discards_new_delta_items() -> None:
    body = {
        "input": [
            {"type": "message", "role": "user", "content": "x" * 8_000},
            {"type": "message", "role": "user", "content": "latest"},
        ]
    }
    with pytest.raises(BridgePromptTooLargeError):
        render_bridge_prompt(
            body,
            "nonce",
            continuation=True,
            max_bytes=4_096,
        )


def test_codex_thread_key_prefers_per_agent_metadata_thread_id() -> None:
    body = {
        "prompt_cache_key": "per-agent-prompt-key",
        "client_metadata": {"thread_id": "metadata-thread-id"},
    }
    assert codex_thread_key(body) == "metadata-thread-id"
    assert codex_thread_key_hash(body) == (
        "f80d467dd2fcd457a7c885605ee159a8bd09a3b2e349b8ed056b15bbf7de4512"
    )


def test_codex_thread_key_falls_back_to_prompt_cache_key() -> None:
    body = {"prompt_cache_key": "per-agent-prompt-key"}
    assert codex_thread_key(body) == "per-agent-prompt-key"
    assert len(codex_thread_key_hash(body) or "") == 64


def test_reasoning_effort_maps_to_kiro_cli_levels() -> None:
    assert kiro_effort_from_body({"reasoning": {"effort": "low"}}) == "low"
    assert kiro_effort_from_body({"reasoning": {"effort": "xhigh"}}) == "xhigh"
    assert kiro_effort_from_body({"reasoning": {"effort": "max"}}) == "max"
    assert kiro_effort_from_body({"reasoning": {"effort": "ultra"}}) == "max"
    assert kiro_effort_from_body({"reasoning": {"effort": "minimal"}}) == "low"
    assert kiro_effort_from_body({"input": "hi"}) is None


def test_streaming_message_parser_decodes_split_json_and_terminal_codes() -> None:
    wire = (
        "\x1b[m> \x1b[0mCODEX_SWITCHBOARD_BRIDGE_BEGIN_n\x1b[0m\n"
        '{"kind":"message","text":"line 1\\n你好 '
        '\\ud83d\\ude80 \\"quoted\\""}\x1b[0m\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    parser = StreamingMessageParser("n")
    deltas = [parser.feed(char) for char in wire]

    assert parser.started is True
    assert parser.done is True
    assert parser.error is None
    assert parser.text == 'line 1\n你好 🚀 "quoted"'
    assert "".join(deltas) == parser.text


def test_streaming_parser_does_not_buffer_short_non_marker_text() -> None:
    parser = StreamingMessageParser("n")
    delta = parser.feed(
        'CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n{"kind":"message","text":"续接成功'
    )

    assert delta == "续接成功"
    assert parser.done is False


def test_streaming_parser_releases_commentary_but_not_tool_payload() -> None:
    commentary = "I will read the configuration, then run the checks."
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps(
            {
                "kind": "tool_calls",
                "commentary": commentary,
                "calls": [{"name": "shell", "payload": "SECRET_TOOL_PAYLOAD"}],
            },
            ensure_ascii=False,
        )
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    parser = StreamingMessageParser("n")
    visible = "".join(parser.feed(char) for char in wire)

    assert parser.phase == "commentary"
    assert parser.done is True
    assert parser.text == commentary
    assert visible == commentary
    assert "SECRET_TOOL_PAYLOAD" not in visible


@pytest.mark.parametrize(
    "message",
    [
        "The context window has overflowed, summarizing the history...",
        (
            "The context window has overflowed, summarizing the history...\n\n"
            "> CODEX_SWITCHBOARD_B...content truncated due to length"
        ),
        "CODEX_SWITCHBOARD_B...content truncated due to length",
        "CODEX_SWITCHBOARD_BRIDGE...content truncated due to length",
    ],
)
def test_streaming_parser_withholds_retryable_status_at_every_boundary(
    message: str,
) -> None:
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps({"kind": "message", "text": message})
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    for split_at in range(1, len(wire)):
        parser = StreamingMessageParser("n")
        visible = parser.feed(wire[:split_at]) + parser.feed(wire[split_at:])
        assert visible == ""
        assert parser.done is True
        assert parser.text == message
        with pytest.raises(BridgeUpstreamRetryableError):
            parse_bridge_output(wire, [], "n")


def test_streaming_parser_releases_text_that_only_shares_status_prefix() -> None:
    message = "The context window has room for this normal answer."
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_n\n"
        + json.dumps({"kind": "message", "text": message})
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_n"
    )
    parser = StreamingMessageParser("n")
    visible = "".join(parser.feed(char) for char in wire)

    assert visible == message
    assert parser.done is True


def test_streaming_parser_blocks_nested_marker_at_every_chunk_boundary() -> None:
    stale = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_old\n"
        '{"kind":"tool_calls","calls":[]}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_old"
    )
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_current\n"
        + json.dumps({"kind": "message", "text": stale})
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_current"
    )
    for split_at in range(1, len(wire)):
        parser = StreamingMessageParser("current")
        visible = parser.feed(wire[:split_at]) + parser.feed(wire[split_at:])
        assert visible == ""
        assert parser.protocol_contaminated is True
        assert parser.text == stale


def test_streaming_commentary_blocks_old_nonce_at_every_chunk_boundary() -> None:
    stale = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_old\n"
        '{"kind":"message","text":"old"}\n'
        "CODEX_SWITCHBOARD_BRIDGE_END_old"
    )
    wire = (
        "CODEX_SWITCHBOARD_BRIDGE_BEGIN_current\n"
        + json.dumps(
            {
                "kind": "tool_calls",
                "commentary": stale,
                "calls": [{"name": "shell", "payload": "pwd"}],
            }
        )
        + "\nCODEX_SWITCHBOARD_BRIDGE_END_current"
    )
    for split_at in range(1, len(wire)):
        parser = StreamingMessageParser("current")
        visible = parser.feed(wire[:split_at]) + parser.feed(wire[split_at:])
        assert visible == ""
        assert parser.protocol_contaminated is True
        assert parser.phase == "commentary"
    with pytest.raises(BridgeProtocolError, match="Nested"):
        parse_bridge_output(wire, [{"type": "custom", "name": "shell"}], "current")
