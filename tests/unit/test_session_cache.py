import asyncio
import hashlib
import json
import logging
import stat
import time

from codex_provider_switchboard.infrastructure.session_cache import SessionCache


def _user(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _assistant(text: str, *, response_shape: bool) -> dict:
    item = {
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }
    if response_shape:
        item.update({"id": "msg_generated", "status": "completed"})
    return item


def test_same_codex_thread_resumes_kiro_with_only_new_input(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first_input = [_user("first")]
        first = await cache.acquire(
            {"prompt_cache_key": "main-agent-thread", "input": first_input}
        )
        try:
            assert first.reusable is True
            assert first.is_resume is False
            first.commit(
                [_assistant("remembered", response_shape=True)],
                "kiro-session-1",
            )
            assert first.state_path is not None
            assert stat.S_IMODE(first.state_path.stat().st_mode) == 0o600
        finally:
            await first.close()

        new_user = _user("second")
        second_input = [
            *first_input,
            _assistant("remembered", response_shape=False),
            new_user,
        ]
        second = await cache.acquire(
            {"prompt_cache_key": "main-agent-thread", "input": second_input}
        )
        try:
            assert second.continuation is True
            assert second.resume_id == "kiro-session-1"
            assert second.resume_latest is False
            assert second.request_body["input"] == [new_user]
        finally:
            await second.close()

    asyncio.run(scenario())


def test_main_agent_and_subagent_keys_use_separate_workdirs(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        main = await cache.acquire(
            {
                "prompt_cache_key": "shared-agent-tree",
                "client_metadata": {"thread_id": "main-thread"},
                "input": [_user("main")],
            }
        )
        subagent = await cache.acquire(
            {
                "prompt_cache_key": "shared-agent-tree",
                "client_metadata": {"thread_id": "subagent-thread"},
                "input": [_user("sub")],
            }
        )
        try:
            assert main.workdir != subagent.workdir
            assert main.workdir.parent == subagent.workdir.parent
            assert len(main.workdir.name) == 64
            assert len(subagent.workdir.name) == 64
            assert "main-thread" not in str(main.workdir)
            assert "subagent-thread" not in str(subagent.workdir)
        finally:
            await main.close()
            await subagent.close()

    asyncio.run(scenario())


def test_context_mismatch_starts_a_new_kiro_session(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first = await cache.acquire(
            {"prompt_cache_key": "thread", "input": [_user("old context")]}
        )
        try:
            first.commit(
                [_assistant("old answer", response_shape=True)],
                "stale-session",
            )
        finally:
            await first.close()

        replacement_body = {
            "prompt_cache_key": "thread",
            "input": [_user("compacted or replaced context")],
        }
        replacement = await cache.acquire(replacement_body)
        try:
            assert replacement.continuation is False
            assert replacement.is_resume is False
            assert replacement.request_body == replacement_body
        finally:
            await replacement.close()

    asyncio.run(scenario())


def test_expired_mapping_does_not_resume(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=1)
        first_input = [_user("first")]
        first = await cache.acquire(
            {"prompt_cache_key": "thread", "input": first_input}
        )
        try:
            first.commit(
                [_assistant("answer", response_shape=True)],
                "expired-session",
            )
            assert first.state_path is not None
            state = json.loads(first.state_path.read_text(encoding="utf-8"))
            state["updated_at"] = time.time() - 10
            first.state_path.write_text(json.dumps(state), encoding="utf-8")
        finally:
            await first.close()

        body = {
            "prompt_cache_key": "thread",
            "input": [
                *first_input,
                _assistant("answer", response_shape=False),
                _user("next"),
            ],
        }
        expired = await cache.acquire(body)
        try:
            assert expired.continuation is False
            assert expired.is_resume is False
            assert expired.request_body == body
        finally:
            await expired.close()

    asyncio.run(scenario())


def test_mapping_falls_back_to_latest_session_when_id_is_unavailable(
    tmp_path,
) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first_input = [_user("first")]
        first = await cache.acquire(
            {"prompt_cache_key": "thread", "input": first_input}
        )
        try:
            first.commit([_assistant("answer", response_shape=True)], None)
        finally:
            await first.close()

        second = await cache.acquire(
            {
                "prompt_cache_key": "thread",
                "input": [
                    *first_input,
                    _assistant("answer", response_shape=False),
                    _user("next"),
                ],
            }
        )
        try:
            assert second.continuation is True
            assert second.resume_id is None
            assert second.resume_latest is True
        finally:
            await second.close()

    asyncio.run(scenario())


def test_contaminated_mapping_is_deleted_before_fresh_retry(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first_input = [_user("first")]
        first = await cache.acquire(
            {"prompt_cache_key": "thread", "input": first_input}
        )
        try:
            first.commit([_assistant("answer", response_shape=True)], "stale-session")
        finally:
            await first.close()

        body = {
            "prompt_cache_key": "thread",
            "input": [
                *first_input,
                _assistant("answer", response_shape=False),
                _user("next"),
            ],
        }
        resumed = await cache.acquire(body)
        try:
            assert resumed.is_resume is True
            assert resumed.state_path is not None and resumed.state_path.exists()
            resumed.discard_mapping_for_retry()
            assert resumed.state_path.exists() is False
            assert resumed.is_resume is False
            assert resumed.continuation is False
            assert resumed.request_body == body
        finally:
            await resumed.close()

    asyncio.run(scenario())


def test_continuation_reports_client_tool_gap(tmp_path, caplog) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first_input = [_user("run a tool")]
        first = await cache.acquire(
            {"prompt_cache_key": "gap-thread", "input": first_input}
        )
        tool_call = {
            "type": "custom_tool_call",
            "call_id": "call-1",
            "name": "exec",
            "input": "text(true);",
        }
        try:
            first.commit([tool_call], "kiro-session")
            assert first.state_path is not None
            state = json.loads(first.state_path.read_text())
            state["awaiting_tool_since"] = time.time() - 75
            first.state_path.write_text(json.dumps(state))
        finally:
            await first.close()

        with caplog.at_level(
            logging.INFO,
            logger="codex_provider_switchboard.infrastructure.session_cache",
        ):
            second = await cache.acquire(
                {
                    "prompt_cache_key": "gap-thread",
                    "input": [
                        *first_input,
                        tool_call,
                        {
                            "type": "custom_tool_call_output",
                            "call_id": "call-1",
                            "output": "done",
                        },
                    ],
                }
            )
        try:
            assert second.continuation is True
            assert second.client_tool_gap_ms is not None
            assert second.client_tool_gap_ms >= 74_000
            assert len(second.client_tool_calls) == 1
            metadata = second.client_tool_calls[0]
            assert metadata.item_type == "custom_tool_call"
            assert metadata.tool_name == "exec"
            expected = hashlib.sha256(b"call-1").hexdigest()[:16]
            assert metadata.call_id_hash == expected
            assert expected in caplog.text
            assert "text(true)" not in caplog.text
            assert "done" not in caplog.text
        finally:
            await second.close()

    asyncio.run(scenario())


def test_pending_tool_diagnostics_are_bounded_and_payload_free(
    tmp_path, caplog
) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        lease = await cache.acquire(
            {
                "prompt_cache_key": "privacy-thread",
                "input": [_user("private user text")],
            }
        )
        calls = [
            {
                "type": ("function_call" if index % 2 == 0 else "custom_tool_call"),
                "call_id": f"secret-call-{index}",
                "name": f"tool_{index}",
                "arguments": "private arguments",
                "input": "private input",
            }
            for index in range(40)
        ]
        try:
            with caplog.at_level(
                logging.INFO,
                logger="codex_provider_switchboard.infrastructure.session_cache",
            ):
                lease.commit(calls, "provider-session")
            assert lease.state_path is not None
            state = json.loads(lease.state_path.read_text())
            assert len(state["pending_tool_calls"]) == 32
            serialized = json.dumps(state)
            for secret in (
                "secret-call-0",
                "private arguments",
                "private input",
                "private user text",
            ):
                assert secret not in serialized
                assert secret not in caplog.text
            expected = hashlib.sha256(b"secret-call-0").hexdigest()[:16]
            assert state["pending_tool_calls"][0] == {
                "item_type": "function_call",
                "tool_name": "tool_0",
                "call_id_hash": expected,
            }
            assert state["pending_tool_calls"][1]["item_type"] == "custom_tool_call"
            assert expected in caplog.text
        finally:
            await lease.close()

    asyncio.run(scenario())


def test_old_v1_state_without_pending_metadata_still_loads(tmp_path) -> None:
    async def scenario() -> None:
        cache = SessionCache(tmp_path, enabled=True, ttl_seconds=3600)
        first_input = [_user("first")]
        first = await cache.acquire(
            {"prompt_cache_key": "old-state-thread", "input": first_input}
        )
        tool_call = {
            "type": "function_call",
            "call_id": "legacy-call",
            "name": "legacy_tool",
            "arguments": "{}",
        }
        try:
            first.commit([tool_call], "legacy-session")
            assert first.state_path is not None
            state = json.loads(first.state_path.read_text())
            state.pop("pending_tool_calls")
            state["awaiting_tool_since"] = time.time() - 2
            first.state_path.write_text(json.dumps(state))
        finally:
            await first.close()

        second = await cache.acquire(
            {
                "prompt_cache_key": "old-state-thread",
                "input": [
                    *first_input,
                    tool_call,
                    {
                        "type": "function_call_output",
                        "call_id": "legacy-call",
                        "output": "legacy output",
                    },
                ],
            }
        )
        try:
            assert second.continuation is True
            assert second.client_tool_gap_ms is not None
            assert second.client_tool_calls == ()
        finally:
            await second.close()

    asyncio.run(scenario())
