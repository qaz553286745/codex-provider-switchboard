from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

_CONTROL_TOOLS = frozenset(
    {
        "followup_task",
        "interrupt_agent",
        "list_agents",
        "send_message",
        "spawn_agent",
        "wait_agent",
    }
)

AGENT_ORCHESTRATION_GUIDANCE = """Subagent orchestration safety:
- Delegate only when the user or applicable project or skill instructions request it.
- A Codex ``NEW_TASK`` inter-agent message is the child's active task. Treat inherited
  parent turns as background and never resume an older task in place of it.
- Let running agents work. If ``wait_agent`` is available, use it for waiting instead
  of polling repeatedly with ``list_agents``.
- ``interrupt_agent`` stops a target. After a target is interrupted, do not interrupt
  it again unless a new user instruction materially changes the task.
- ``followup_task`` starts another turn for an idle, completed, or interrupted target.
  Never use it as polling or merely to undo an interrupt from the same user turn.
- Never alternate ``interrupt_agent`` and ``followup_task`` for the same target. If
  orchestration state is unclear, stop issuing control calls and report the status.
"""


@dataclass(frozen=True, slots=True)
class AgentControlLoop:
    reason: str
    control_calls: int
    restart_count: int
    target_digest: str | None

    @property
    def user_message(self) -> str:
        return (
            "[provider-switchboard] Stopped a repeated subagent-control loop. "
            "The same Codex turn was interrupting and restarting agents without "
            "new user instructions or substantive tool progress. This turn ended "
            "normally to prevent further token usage. Send a new instruction if "
            "you intentionally want to resume one agent."
        )


@dataclass(frozen=True, slots=True)
class _ControlCall:
    name: str
    target: str | None
    signature: str


def _tool_name(item: dict[str, Any]) -> str | None:
    name = item.get("name")
    if not isinstance(name, str):
        return None
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace:
        prefix = f"{namespace}."
        if not name.startswith(prefix):
            name = f"{prefix}{name}"
    bare = name.rsplit(".", 1)[-1]
    return bare if bare in _CONTROL_TOOLS else None


def _arguments(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("arguments")
    if raw is None:
        raw = item.get("input")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _call_signature(name: str, arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        encoded = "[unavailable]"
    return _safe_digest(f"{name}\0{encoded}")


def _trailing_control_calls(body: dict[str, Any]) -> list[_ControlCall]:
    """Return the control-only suffix of the active user turn.

    A new user message or a substantive non-control tool call starts a fresh
    observation window. Tool outputs are ignored: they are evidence that Codex
    received a call result, not new authority to restart an interrupted agent.
    """

    input_value = body.get("input")
    if not isinstance(input_value, list):
        return []

    calls: list[_ControlCall] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {None, "message"} and item.get("role") == "user":
            calls.clear()
            continue
        if item.get("type") == "agent_message":
            # A child result or a new delegated task is substantive orchestration
            # progress and starts a fresh control window.
            calls.clear()
            continue
        if item.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        name = _tool_name(item)
        if name is None:
            calls.clear()
            continue
        arguments = _arguments(item)
        raw_target = arguments.get("target")
        target = raw_target if isinstance(raw_target, str) and raw_target else None
        calls.append(
            _ControlCall(
                name=name,
                target=target,
                signature=_call_signature(name, arguments),
            )
        )
    return calls


def has_agent_control_tools(tools: list[dict[str, Any]]) -> bool:
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if _tool_name(tool) is not None:
            return True
        children = tool.get("tools")
        if (
            tool.get("type") == "namespace"
            and isinstance(children, list)
            and has_agent_control_tools(children)
        ):
            return True
    return False


def detect_agent_control_loop(
    body: dict[str, Any], *, restart_limit: int = 2
) -> AgentControlLoop | None:
    """Detect bounded, high-confidence subagent control loops.

    The guard intentionally ignores ordinary waiting and one legitimate
    interrupt/restart correction. It trips only after repeated restart cycles,
    repeated identical mutating control calls, or sustained control churn.
    """

    restart_limit = max(1, restart_limit)
    calls = _trailing_control_calls(body)
    if not calls:
        return None

    actions_by_target: dict[str, list[str]] = defaultdict(list)
    for call in calls:
        if call.target is not None and call.name in {
            "followup_task",
            "interrupt_agent",
        }:
            actions_by_target[call.target].append(call.name)

    total_restarts = 0
    highest_restarts = 0
    highest_target: str | None = None
    for target, actions in actions_by_target.items():
        interrupted = False
        restarted = False
        restart_interruptions = 0
        for action in actions:
            if action == "followup_task":
                if interrupted:
                    restarted = True
                continue
            if restarted:
                restart_interruptions += 1
                restarted = False
            interrupted = True
        total_restarts += restart_interruptions
        if restart_interruptions > highest_restarts:
            highest_restarts = restart_interruptions
            highest_target = target

    if highest_restarts >= restart_limit:
        return AgentControlLoop(
            reason="interrupt_followup_cycle",
            control_calls=len(calls),
            restart_count=highest_restarts,
            target_digest=_safe_digest(highest_target) if highest_target else None,
        )

    signatures = Counter((call.name, call.signature) for call in calls)
    repeated_limits = {
        "followup_task": max(3, restart_limit + 1),
        "interrupt_agent": max(3, restart_limit + 1),
        "list_agents": 8,
        "send_message": 5,
    }
    for (name, _signature), count in signatures.items():
        limit = repeated_limits.get(name)
        if limit is not None and count >= limit:
            target = next(
                (call.target for call in calls if call.name == name),
                None,
            )
            return AgentControlLoop(
                reason=f"repeated_{name}",
                control_calls=len(calls),
                restart_count=0,
                target_digest=_safe_digest(target) if target else None,
            )

    counts = Counter(call.name for call in calls)
    churn_limit = max(12, restart_limit * 6)
    if (
        len(calls) >= churn_limit
        and counts["interrupt_agent"] >= restart_limit * 3
        and counts["followup_task"] >= restart_limit
    ):
        return AgentControlLoop(
            reason="agent_control_churn",
            control_calls=len(calls),
            restart_count=total_restarts,
            target_digest=(
                _safe_digest(highest_target) if highest_target is not None else None
            ),
        )
    return None
