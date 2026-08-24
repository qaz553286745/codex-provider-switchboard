from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import __version__
from ..compatibility.profiles import compatibility_profile
from ..compatibility.responses import (
    AdaptedResponsesRequest,
    ResponsesCompatibilityError,
    ResponsesStreamRestorer,
    adapt_responses_request,
    forwarded_codex_headers,
    prepare_compaction_request,
    restore_response_value,
)
from ..compatibility.sse import (
    ResponsesSSEDecoder,
    ResponsesSSEError,
    encode_response_event,
)
from .config_store import ConfigStore

_MAX_JSON_BYTES = 4 * 1_048_576
_MAX_STREAM_BYTES = 64 * 1_048_576


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class CustomAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _dig(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else ():
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _first(value: Any, paths: list[str]) -> Any:
    for path in paths:
        candidate = _dig(value, path)
        if candidate is not None:
            return candidate
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.replace(",", "").strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


class CustomResponsesClient:
    """Bounded client for a user-configured OpenAI Responses-compatible API."""

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

    def _connection(self) -> tuple[dict[str, Any], str]:
        config = self.store.read()["custom"]
        base_url = str(config.get("base_url") or "")
        api_key = self.store.custom_api_key()
        if not base_url:
            raise CustomAPIError("Third-party base URL is not configured.")
        if not api_key:
            raise CustomAPIError("Third-party API key is not configured.")
        return config, api_key

    def _client(self, extra_headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        config, api_key = self._connection()
        timeout = int(config["timeout_seconds"])
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"codex-provider-switchboard/{__version__}",
        }
        if extra_headers:
            headers.update(extra_headers)
        return httpx.AsyncClient(
            base_url=str(config["base_url"]),
            headers=headers,
            timeout=httpx.Timeout(connect=20, read=timeout, write=60, pool=20),
            follow_redirects=False,
            transport=self.transport,
        )

    @staticmethod
    async def _read_limited(
        response: httpx.Response, limit: int = _MAX_JSON_BYTES
    ) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise CustomAPIError(
                        "Third-party response exceeded the byte limit."
                    )
            except ValueError:
                pass
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                raise CustomAPIError("Third-party response exceeded the byte limit.")
        return bytes(raw)

    @staticmethod
    def _error_from_bytes(
        status_code: int,
        raw: bytes,
    ) -> CustomAPIError:
        message = f"Third-party API returned HTTP {status_code}."
        try:
            payload = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if isinstance(detail, str) and detail.strip():
                message = f"Third-party API: {detail.strip()[:500]}"
        return CustomAPIError(message, status_code=status_code)

    async def _request_json(self, method: str, path: str) -> Any:
        try:
            async with (
                self._client() as client,
                client.stream(method, path) as response,
            ):
                raw = await self._read_limited(response)
        except CustomAPIError:
            raise
        except httpx.HTTPError as exc:
            raise CustomAPIError(
                f"Could not reach third-party API: {type(exc).__name__}."
            ) from exc
        if response.status_code >= 400:
            raise self._error_from_bytes(response.status_code, raw)
        try:
            return json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CustomAPIError("Third-party API returned invalid JSON.") from exc

    def _response_body(
        self, body: dict[str, Any], *, stream: bool
    ) -> tuple[AdaptedResponsesRequest, dict[str, str]]:
        config, _ = self._connection()
        capabilities = compatibility_profile(
            str(config.get("compatibility_profile") or "function_only")
        )
        try:
            adapted = adapt_responses_request(body, capabilities)
        except ResponsesCompatibilityError as exc:
            raise CustomAPIError(str(exc), status_code=400) from exc
        payload = adapted.body
        headers = (
            forwarded_codex_headers(body) if capabilities.forward_codex_headers else {}
        )
        if capabilities.native_multi_agent:
            multi_agent = body.get("multi_agent")
            if isinstance(multi_agent, dict) and multi_agent.get("enabled") is True:
                beta = [
                    item.strip()
                    for item in headers.get("OpenAI-Beta", "").split(",")
                    if item.strip()
                ]
                if "responses_multi_agent=v1" not in beta:
                    beta.append("responses_multi_agent=v1")
                headers["OpenAI-Beta"] = ", ".join(beta)
        payload.pop("client_metadata", None)
        model_id = str(config.get("model_id") or "")
        if model_id:
            payload["model"] = model_id
        payload["stream"] = stream
        return AdaptedResponsesRequest(payload, adapted.mapping), headers

    async def create_response(self, body: dict[str, Any]) -> dict[str, Any]:
        adapted, headers = self._response_body(body, stream=False)
        try:
            async with (
                self._client(headers) as client,
                client.stream("POST", "/responses", json=adapted.body) as response,
            ):
                raw = await self._read_limited(response)
        except CustomAPIError:
            raise
        except httpx.HTTPError as exc:
            raise CustomAPIError(
                f"Third-party Responses request failed: {type(exc).__name__}."
            ) from exc
        if response.status_code >= 400:
            raise self._error_from_bytes(response.status_code, raw)
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CustomAPIError(
                "Third-party API returned invalid Responses JSON."
            ) from exc
        if not isinstance(value, dict):
            raise CustomAPIError("Third-party API returned an unexpected JSON value.")
        return restore_response_value(value, adapted.mapping)

    async def compact_response(self, body: dict[str, Any]) -> dict[str, Any]:
        config, _ = self._connection()
        capabilities = compatibility_profile(
            str(config.get("compatibility_profile") or "function_only")
        )
        if not capabilities.native_compaction:
            raise CustomAPIError(
                "The configured third-party compatibility profile does not support "
                "native Responses compaction.",
                status_code=400,
            )
        model_id = str(config.get("model_id") or body.get("model") or "")
        try:
            payload = prepare_compaction_request(body, model=model_id)
        except ResponsesCompatibilityError as exc:
            raise CustomAPIError(str(exc), status_code=400) from exc
        headers = forwarded_codex_headers(body)
        try:
            async with (
                self._client(headers) as client,
                client.stream("POST", "/responses/compact", json=payload) as response,
            ):
                raw = await self._read_limited(response)
        except CustomAPIError:
            raise
        except httpx.HTTPError as exc:
            raise CustomAPIError(
                f"Third-party compaction request failed: {type(exc).__name__}."
            ) from exc
        if response.status_code >= 400:
            raise self._error_from_bytes(response.status_code, raw)
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CustomAPIError(
                "Third-party API returned invalid compaction JSON."
            ) from exc
        if not isinstance(value, dict):
            raise CustomAPIError(
                "Third-party API returned an unexpected compaction value."
            )
        return value

    async def stream_response(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        adapted, forwarded_headers = self._response_body(body, stream=True)
        restorer = (
            None if adapted.mapping.empty else ResponsesStreamRestorer(adapted.mapping)
        )
        decoder = ResponsesSSEDecoder(event_limit=_MAX_JSON_BYTES)
        stream_bytes = 0
        try:
            async with (
                self._client(forwarded_headers) as client,
                client.stream(
                    "POST",
                    "/responses",
                    json=adapted.body,
                    headers={"Accept": "text/event-stream"},
                ) as response,
            ):
                if response.status_code >= 400:
                    raw = await self._read_limited(response)
                    raise self._error_from_bytes(response.status_code, raw)
                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    raise CustomAPIError(
                        "Third-party streaming endpoint did not return SSE."
                    )
                async for chunk in response.aiter_bytes():
                    stream_bytes += len(chunk)
                    if stream_bytes > _MAX_STREAM_BYTES:
                        raise CustomAPIError(
                            "Third-party SSE stream exceeded the byte limit."
                        )
                    if chunk:
                        if restorer is None:
                            yield chunk
                            continue
                        for event in decoder.feed(chunk):
                            for restored in restorer.restore(event):
                                yield encode_response_event(restored)
                if restorer is not None:
                    decoder.finish()
        except CustomAPIError:
            raise
        except ResponsesSSEError as exc:
            raise CustomAPIError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise CustomAPIError(
                f"Third-party Responses stream failed: {type(exc).__name__}."
            ) from exc

    @staticmethod
    def _normalize_models(payload: Any) -> list[dict[str, Any]]:
        raw_items: Any = payload
        if isinstance(payload, dict):
            raw_items = payload.get("data")
            if not isinstance(raw_items, list):
                raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raw_items = payload.get("models")
        if not isinstance(raw_items, list):
            raise CustomAPIError("Third-party model catalog did not contain a list.")

        result: list[dict[str, Any]] = []
        for item in raw_items[:1_000]:
            if isinstance(item, str):
                model_id = item.strip()
                display_name = model_id
                description = ""
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("model") or item.get("name")
                display_name = (
                    item.get("displayName")
                    or item.get("display_name")
                    or item.get("name")
                    or model_id
                )
                description = item.get("description") or ""
            else:
                continue
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            normalized: dict[str, Any] = {
                "id": model_id.strip()[:200],
                "displayName": str(display_name)[:240],
            }
            if isinstance(description, str) and description.strip():
                normalized["description"] = description.strip()[:1_000]
            result.append(normalized)
        if not result:
            raise CustomAPIError("Third-party model catalog was empty.")
        return result

    async def get_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        config, api_key = self._connection()
        cache_key = hashlib.sha256(
            (
                str(config["base_url"])
                + "\0"
                + str(config["models_path"])
                + "\0"
                + api_key
            ).encode()
        ).hexdigest()
        now = time.monotonic()
        cached = self._models_cache
        if not force and cached and cached[0] == cache_key and now - cached[1] < 300:
            return [dict(item) for item in cached[2]]

        async with self._models_lock:
            cached = self._models_cache
            now = time.monotonic()
            if (
                not force
                and cached
                and cached[0] == cache_key
                and now - cached[1] < 300
            ):
                return [dict(item) for item in cached[2]]
            payload = await self._request_json("GET", str(config["models_path"]))
            items = self._normalize_models(payload)
            self._models_cache = (cache_key, now, items)
            return [dict(item) for item in items]

    async def quota(self) -> dict[str, Any]:
        config, _ = self._connection()
        endpoint = str(config.get("quota_path") or "")
        if not endpoint:
            return {
                "status": "unsupported",
                "source": "not configured",
                "note": "Configure a same-origin quota endpoint and JSON field paths.",
            }
        payload = await self._request_json("GET", endpoint)
        configured = {
            "total": str(config.get("quota_total_field") or ""),
            "used": str(config.get("quota_used_field") or ""),
            "remaining": str(config.get("quota_remaining_field") or ""),
            "reset_at": str(config.get("quota_reset_field") or ""),
        }
        total = _number(
            _first(
                payload,
                [configured["total"]]
                if configured["total"]
                else [
                    "total",
                    "limit",
                    "quota.total",
                    "data.total",
                    "credits.total",
                ],
            )
        )
        used = _number(
            _first(
                payload,
                [configured["used"]]
                if configured["used"]
                else [
                    "used",
                    "usage",
                    "quota.used",
                    "data.used",
                    "credits.used",
                ],
            )
        )
        remaining = _number(
            _first(
                payload,
                [configured["remaining"]]
                if configured["remaining"]
                else [
                    "remaining",
                    "balance",
                    "quota.remaining",
                    "data.remaining",
                    "credits.remaining",
                ],
            )
        )
        if total is not None and used is None and remaining is not None:
            used = total - remaining
        if total is not None and remaining is None and used is not None:
            remaining = total - used
        reset_at = _first(
            payload,
            [configured["reset_at"]]
            if configured["reset_at"]
            else [
                "reset_at",
                "resetAt",
                "quota.reset_at",
                "data.reset_at",
            ],
        )
        mapped = any(value is not None for value in (total, used, remaining))
        result: dict[str, Any] = {
            "status": "available" if mapped else "unmapped",
            "source": endpoint,
            "total": total,
            "used": used,
            "remaining": remaining,
            "unit": str(config.get("quota_unit") or "credits")[:100],
        }
        if isinstance(reset_at, (str, int, float)) and not isinstance(reset_at, bool):
            result["reset_at"] = str(reset_at)[:200]
        if not mapped:
            result["note"] = "Endpoint is reachable, but quota fields were not mapped."
        return result
