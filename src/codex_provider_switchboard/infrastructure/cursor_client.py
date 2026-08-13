from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .. import __version__
from .config_store import ConfigStore

_MAX_JSON_RESPONSE_BYTES = 4 * 1_048_576
_MAX_SSE_EVENT_BYTES = 8 * 1_048_576
_MAX_SSE_STREAM_BYTES = 32 * 1_048_576


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class CursorBackendError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CursorAPIError(CursorBackendError):
    """Failure returned by the optional Cursor Cloud Agents API backend."""


@dataclass(frozen=True)
class CursorModelSelection:
    model_id: str
    params: tuple[tuple[str, Any], ...]
    display_name: str
    context_window_tokens: int | None = field(default=None, compare=False)

    @property
    def request_value(self) -> dict[str, Any] | None:
        if not self.model_id:
            return None
        value: dict[str, Any] = {"id": self.model_id}
        if self.params:
            value["params"] = [
                {"id": param_id, "value": param_value}
                for param_id, param_value in self.params
            ]
        return value

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.request_value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]


@dataclass(frozen=True)
class CursorRun:
    agent_id: str
    run_id: str
    is_continuation: bool
    reported_model: str | None = field(default=None, compare=False)
    state: object | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class CursorStreamEvent:
    event: str
    data: dict[str, Any]
    event_id: str | None = None


def requested_effort(body: dict[str, Any]) -> str | None:
    reasoning = body.get("reasoning")
    value: Any = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if not isinstance(value, str):
        value = body.get("reasoning_effort")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("-", "_")


def _catalog_model(items: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        aliases = item.get("aliases")
        if item.get("id") == model_id or (
            isinstance(aliases, list) and model_id in aliases
        ):
            return item
    return None


def _allowed_values(parameter: dict[str, Any]) -> list[Any]:
    values = parameter.get("values")
    if not isinstance(values, list):
        return []
    return [item.get("value") for item in values if isinstance(item, dict)]


def _matching_allowed(allowed: list[Any], candidates: list[str]) -> Any | None:
    for candidate in candidates:
        for value in allowed:
            if str(value).strip().lower().replace("-", "_") == candidate:
                return value
    return None


def apply_codex_effort(
    params: list[dict[str, Any]],
    model: dict[str, Any] | None,
    requested_effort: str | None,
) -> list[dict[str, Any]]:
    """Apply effort only through parameter IDs and values advertised by Cursor."""
    result = [dict(item) for item in params]
    if model is None or requested_effort is None:
        return result

    normalized = {
        "extra_high": "xhigh",
        "ultra": "max",
    }.get(requested_effort, requested_effort)
    effort_candidates = {
        "none": ["none", "minimal", "low"],
        "minimal": ["minimal", "low", "none"],
        "low": ["low", "minimal"],
        "medium": ["medium"],
        "high": ["high"],
        "xhigh": ["xhigh", "extra_high", "max", "high"],
        "max": ["max", "ultra", "xhigh", "extra_high", "high"],
    }.get(normalized, [normalized])

    by_id = {str(item.get("id")): item for item in result if item.get("id")}
    parameters = model.get("parameters")
    if not isinstance(parameters, list):
        return result

    for parameter in parameters:
        if not isinstance(parameter, dict) or not isinstance(parameter.get("id"), str):
            continue
        param_id = parameter["id"]
        compact = re.sub(r"[^a-z0-9]", "", param_id.lower())
        allowed = _allowed_values(parameter)
        selected: Any | None = None
        if "effort" in compact or compact in {
            "reasoning",
            "reasoninglevel",
            "thinkingeffort",
        }:
            selected = _matching_allowed(allowed, effort_candidates)
        elif compact in {"max", "maxmode", "reasoningmax", "usemax"} and (
            normalized in {"max", "xhigh"}
        ):
            selected = _matching_allowed(allowed, ["true", "1", "on", "max"])

        if selected is not None:
            by_id[param_id] = {"id": param_id, "value": selected}

    ordered_ids = [str(item.get("id")) for item in result if item.get("id")]
    for param_id in by_id:
        if param_id not in ordered_ids:
            ordered_ids.append(param_id)
    return [by_id[param_id] for param_id in ordered_ids]


def selection_from_config(
    cursor_config: dict[str, Any],
    body: dict[str, Any],
    models: list[dict[str, Any]] | None = None,
) -> CursorModelSelection:
    model_id = str(cursor_config.get("model_id") or "")
    raw_params = cursor_config.get("model_params")
    params = [dict(item) for item in raw_params] if isinstance(raw_params, list) else []
    model = _catalog_model(models or [], model_id) if model_id else None
    if cursor_config.get("follow_codex_effort") is True:
        params = apply_codex_effort(params, model, requested_effort(body))
    return CursorModelSelection(
        model_id=model_id,
        params=tuple((str(item["id"]), item["value"]) for item in params),
        display_name=str(
            cursor_config.get("model_display_name") or model_id or "Cursor 默认模型"
        ),
    )


class CursorClient:
    backend_id = "cloud_api"
    runtime_name = "Cursor Cloud Agent"
    session_name = "Cursor agent"

    def __init__(
        self,
        store: ConfigStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.transport = transport
        self._models_cache: tuple[str, float, list[dict[str, Any]]] | None = None
        self._models_lock = asyncio.Lock()
        self._last_usage: dict[str, Any] | None = None

    def _connection(self) -> tuple[str, str, int]:
        config = self.store.read()["cursor"]
        api_key = self.store.api_key()
        if not api_key:
            raise CursorAPIError(
                "Cursor API key is not configured. Open the local control panel first."
            )
        return (
            str(config["base_url"]),
            api_key,
            int(config["timeout_seconds"]),
        )

    def _client(self) -> httpx.AsyncClient:
        base_url, api_key, _ = self._connection()
        return httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(api_key, ""),
            headers={"User-Agent": f"codex-provider-switchboard/{__version__}"},
            timeout=httpx.Timeout(connect=20, read=None, write=30, pool=20),
            transport=self.transport,
        )

    @staticmethod
    async def _read_limited(response: httpx.Response, limit: int) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise CursorAPIError("Cursor API response exceeded the byte limit.")
            except ValueError:
                pass
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                raise CursorAPIError("Cursor API response exceeded the byte limit.")
        return bytes(raw)

    @classmethod
    async def _error_from_response(cls, response: httpx.Response) -> CursorAPIError:
        raw = await cls._read_limited(response, _MAX_JSON_RESPONSE_BYTES)
        message = f"Cursor API returned HTTP {response.status_code}."
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if isinstance(detail, str) and detail.strip():
                message = f"Cursor API: {detail.strip()[:500]}"
        return CursorAPIError(message, status_code=response.status_code)

    async def _request_json(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _, _, timeout_seconds = self._connection()
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client() as client:
                    async with client.stream(method, path, json=json_body) as response:
                        if response.status_code >= 400:
                            raise await self._error_from_response(response)
                        raw = await self._read_limited(
                            response, _MAX_JSON_RESPONSE_BYTES
                        )
        except CursorAPIError:
            raise
        except TimeoutError as exc:
            raise CursorAPIError(
                f"Cursor API timed out after {timeout_seconds} seconds."
            ) from exc
        except httpx.HTTPError as exc:
            raise CursorAPIError(
                f"Could not reach Cursor API: {type(exc).__name__}."
            ) from exc
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CursorAPIError("Cursor API returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise CursorAPIError("Cursor API returned an unexpected JSON value.")
        return value

    async def get_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        api_key = self.store.api_key()
        if not api_key:
            raise CursorAPIError(
                "Cursor API key is not configured. Open the local control panel first."
            )
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cached = self._models_cache
        if not force and cached and cached[0] == key_hash and now - cached[1] < 300:
            return [dict(item) for item in cached[2]]

        async with self._models_lock:
            cached = self._models_cache
            now = time.monotonic()
            if not force and cached and cached[0] == key_hash and now - cached[1] < 300:
                return [dict(item) for item in cached[2]]
            payload = await self._request_json("GET", "/v1/models")
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise CursorAPIError("Cursor model catalog did not contain items.")
            items = [item for item in raw_items if isinstance(item, dict)]
            self._models_cache = (key_hash, now, items)
            return [dict(item) for item in items]

    async def effective_selection(self, body: dict[str, Any]) -> CursorModelSelection:
        config = self.store.read()["cursor"]
        models: list[dict[str, Any]] | None = None
        if (
            config.get("follow_codex_effort") is True
            and config.get("model_id")
            and requested_effort(body) is not None
        ):
            try:
                models = await self.get_models()
            except CursorAPIError:
                models = None
        return selection_from_config(config, body, models)

    async def create_agent(
        self, prompt: str, selection: CursorModelSelection
    ) -> CursorRun:
        body: dict[str, Any] = {
            "prompt": {"text": prompt},
            "name": "Codex local bridge",
            "mode": "agent",
        }
        if selection.request_value is not None:
            body["model"] = selection.request_value
        value = await self._request_json("POST", "/v1/agents", json_body=body)
        agent = value.get("agent")
        run = value.get("run")
        if not isinstance(agent, dict) or not isinstance(run, dict):
            raise CursorAPIError("Cursor did not return the created agent and run.")
        agent_id = agent.get("id")
        run_id = run.get("id")
        if not isinstance(agent_id, str) or not isinstance(run_id, str):
            raise CursorAPIError("Cursor returned invalid agent or run identifiers.")
        return CursorRun(agent_id=agent_id, run_id=run_id, is_continuation=False)

    async def create_run(
        self,
        agent_id: str,
        prompt: str,
        selection: CursorModelSelection | None = None,
    ) -> CursorRun:
        safe_agent_id = quote(agent_id, safe="")
        value = await self._request_json(
            "POST",
            f"/v1/agents/{safe_agent_id}/runs",
            json_body={"prompt": {"text": prompt}, "mode": "agent"},
        )
        run = value.get("run")
        if not isinstance(run, dict) or not isinstance(run.get("id"), str):
            raise CursorAPIError("Cursor did not return the created run.")
        return CursorRun(
            agent_id=agent_id,
            run_id=run["id"],
            is_continuation=True,
        )

    async def stream_run(self, run: CursorRun) -> AsyncIterator[CursorStreamEvent]:
        _, _, timeout_seconds = self._connection()
        safe_agent_id = quote(run.agent_id, safe="")
        safe_run_id = quote(run.run_id, safe="")
        path = f"/v1/agents/{safe_agent_id}/runs/{safe_run_id}/stream"
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client() as client:
                    async with client.stream(
                        "GET", path, headers={"Accept": "text/event-stream"}
                    ) as response:
                        if response.status_code >= 400:
                            raise await self._error_from_response(response)
                        event_type = "message"
                        event_id: str | None = None
                        data_lines: list[str] = []
                        event_bytes = 0
                        stream_bytes = 0

                        async def decoded() -> CursorStreamEvent | None:
                            if not data_lines:
                                return None
                            try:
                                value = json.loads(
                                    "\n".join(data_lines),
                                    parse_constant=_reject_json_constant,
                                )
                            except (json.JSONDecodeError, ValueError) as exc:
                                raise CursorAPIError(
                                    "Cursor sent malformed SSE JSON."
                                ) from exc
                            if not isinstance(value, dict):
                                raise CursorAPIError(
                                    "Cursor sent an unexpected SSE payload."
                                )
                            return CursorStreamEvent(event_type, value, event_id)

                        async for line in response.aiter_lines():
                            if line == "":
                                parsed = await decoded()
                                if parsed is not None:
                                    yield parsed
                                event_type = "message"
                                event_id = None
                                data_lines = []
                                event_bytes = 0
                                continue
                            if line.startswith(":"):
                                continue
                            stream_bytes += len(line.encode()) + 1
                            if stream_bytes > _MAX_SSE_STREAM_BYTES:
                                raise CursorAPIError(
                                    "Cursor SSE stream exceeded the byte limit."
                                )
                            field, _, raw_value = line.partition(":")
                            value = (
                                raw_value[1:]
                                if raw_value.startswith(" ")
                                else raw_value
                            )
                            if field == "event":
                                event_type = value
                            elif field == "id":
                                event_id = value
                            elif field == "data":
                                data_lines.append(value)
                                event_bytes += len(value.encode())
                                if event_bytes > _MAX_SSE_EVENT_BYTES:
                                    raise CursorAPIError(
                                        "Cursor SSE event exceeded the byte limit."
                                    )

                        parsed = await decoded()
                        if parsed is not None:
                            yield parsed
        except TimeoutError as exc:
            raise CursorAPIError(
                f"Cursor run timed out after {timeout_seconds} seconds."
            ) from exc
        except httpx.HTTPError as exc:
            raise CursorAPIError(
                f"Cursor stream failed: {type(exc).__name__}."
            ) from exc

    async def cancel_run(self, run: CursorRun) -> None:
        safe_agent_id = quote(run.agent_id, safe="")
        safe_run_id = quote(run.run_id, safe="")
        try:
            await self._request_json(
                "POST",
                f"/v1/agents/{safe_agent_id}/runs/{safe_run_id}/cancel",
            )
        except CursorAPIError:
            return

    async def usage(self, run: CursorRun) -> dict[str, Any] | None:
        safe_agent_id = quote(run.agent_id, safe="")
        safe_run_id = quote(run.run_id, safe="")
        try:
            value = await self._request_json(
                "GET",
                f"/v1/agents/{safe_agent_id}/usage?runId={safe_run_id}",
            )
        except CursorAPIError:
            return None
        total = value.get("totalUsage")
        if not isinstance(total, dict):
            return None
        input_tokens = int(total.get("inputTokens") or 0)
        output_tokens = int(total.get("outputTokens") or 0)
        cached_tokens = int(total.get("cacheReadTokens") or 0)
        total_tokens = int(total.get("totalTokens") or input_tokens + output_tokens)
        result = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total_tokens,
        }
        self._last_usage = dict(result)
        return result

    async def quota(self, *, force: bool = False) -> dict[str, Any]:
        """Verify the key and report the quota boundary of Cursor's public API."""
        del force
        value = await self._request_json("GET", "/v1/me")
        result: dict[str, Any] = {
            "status": "unsupported",
            "source": "Cursor Cloud Agents API /v1/me",
            "note": (
                "Cursor's public Cloud Agents API does not expose account remaining "
                "quota. Open the Cursor dashboard for the authoritative balance."
            ),
            "dashboard_url": "https://cursor.com/dashboard?tab=usage",
            "account_verified": True,
        }
        for source, target in (
            ("apiKeyName", "api_key_name"),
            ("createdAt", "api_key_created_at"),
        ):
            field = value.get(source)
            if isinstance(field, str) and field.strip():
                result[target] = field.strip()[:240]
        if self._last_usage is not None:
            result["last_run_usage"] = dict(self._last_usage)
        return result
