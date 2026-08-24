from __future__ import annotations

import json

from codex_provider_switchboard.domain.agent_loop_guard import (
    detect_agent_control_loop,
    has_agent_control_tools,
)


def _call(name: str, target: str | None = None, **arguments: object) -> dict:
    payload = dict(arguments)
    if target is not None:
        payload["target"] = target
    return {
        "type": "function_call",
        "name": name,
        "namespace": "collaboration",
        "call_id": f"call-{name}-{len(json.dumps(payload))}",
        "arguments": json.dumps(payload),
    }


def _body(*calls: dict) -> dict:
    return {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue"}],
            },
            *calls,
        ]
    }


def test_one_interrupt_and_restart_correction_is_allowed() -> None:
    body = _body(
        _call("interrupt_agent", "/root/reviewer"),
        _call("followup_task", "/root/reviewer", message="Use the new scope"),
        _call("interrupt_agent", "/root/reviewer"),
    )

    assert detect_agent_control_loop(body) is None


def test_second_interrupt_restart_cycle_is_stopped() -> None:
    target = "/root/reviewer/private-name"
    body = _body(
        _call("interrupt_agent", target),
        _call("followup_task", target, message="Finish the review"),
        _call("interrupt_agent", target),
        _call("followup_task", target, message="Finish the review"),
        _call("interrupt_agent", target),
    )

    decision = detect_agent_control_loop(body)

    assert decision is not None
    assert decision.reason == "interrupt_followup_cycle"
    assert decision.restart_count == 2
    assert decision.target_digest is not None
    assert len(decision.target_digest) == 12
    assert target not in decision.user_message


def test_repeated_identical_mutating_control_call_is_stopped() -> None:
    body = _body(*[_call("interrupt_agent", "/root/reviewer") for _ in range(3)])

    decision = detect_agent_control_loop(body)

    assert decision is not None
    assert decision.reason == "repeated_interrupt_agent"


def test_repeated_list_polling_is_stopped_but_waiting_is_allowed() -> None:
    list_decision = detect_agent_control_loop(
        _body(*[_call("list_agents") for _ in range(8)])
    )
    wait_decision = detect_agent_control_loop(
        _body(*[_call("wait_agent", timeout_ms=30_000) for _ in range(20)])
    )

    assert list_decision is not None
    assert list_decision.reason == "repeated_list_agents"
    assert wait_decision is None


def test_new_user_instruction_and_substantive_work_reset_the_window() -> None:
    old_loop = [
        _call("interrupt_agent", "/root/reviewer"),
        _call("followup_task", "/root/reviewer", message="Retry"),
        _call("interrupt_agent", "/root/reviewer"),
        _call("followup_task", "/root/reviewer", message="Retry"),
        _call("interrupt_agent", "/root/reviewer"),
    ]
    user_reset = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Stop all agents"}],
    }
    body = {"input": [*old_loop, user_reset, _call("list_agents")]}
    assert detect_agent_control_loop(body) is None

    exec_call = {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "call-exec",
        "input": "text(true);",
    }
    body = {"input": [*old_loop, exec_call, _call("list_agents")]}
    assert detect_agent_control_loop(body) is None

    agent_result = {
        "type": "agent_message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Review completed."}],
    }
    body = {"input": [*old_loop, agent_result, _call("list_agents")]}
    assert detect_agent_control_loop(body) is None


def test_agent_tool_catalog_detection_accepts_flat_and_namespaced_names() -> None:
    assert has_agent_control_tools(
        [{"type": "function", "name": "collaboration.followup_task"}]
    )
    assert has_agent_control_tools(
        [
            {
                "type": "function",
                "name": "interrupt_agent",
                "namespace": "collaboration",
            }
        ]
    )
    assert not has_agent_control_tools([{"type": "function", "name": "read_file"}])
