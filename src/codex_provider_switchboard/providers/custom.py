from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from ..application.inspector import RequestInspector
from ..infrastructure.config_store import ConfigStore
from ..infrastructure.custom_client import CustomAPIError, CustomResponsesClient
from .base import ProviderError, ProviderResponse


def _failed_sse(message: str) -> bytes:
    value = {
        "type": "response.failed",
        "response": {
            "id": f"resp_{os.urandom(16).hex()}",
            "object": "response",
            "status": "failed",
            "error": {"code": "custom_api_error", "message": message[:1_000]},
        },
    }
    return f"event: response.failed\ndata: {json.dumps(value)}\n\n".encode()


class CustomProvider:
    provider_id = "custom"

    def __init__(
        self,
        store: ConfigStore,
        client: CustomResponsesClient,
        inspector: RequestInspector,
    ) -> None:
        self.store = store
        self.client = client
        self.inspector = inspector

    def model_id(self) -> str:
        return str(self.store.read()["custom"].get("model_id") or "custom-default")

    def _record(self, action: str) -> None:
        self.inspector.record(
            provider=self.provider_id,
            action=action,
            model=self.model_id(),
            session_reused=False,
        )

    async def complete(self, body: dict[str, Any]) -> ProviderResponse:
        self._record("responses")
        try:
            value = await self.client.create_response(body)
        except CustomAPIError as exc:
            raise ProviderError(
                str(exc),
                error_type="custom_api_error",
                status_code=(
                    exc.status_code
                    if isinstance(exc.status_code, int) and 400 <= exc.status_code < 500
                    else 502
                ),
            ) from exc
        return ProviderResponse(value, {"X-Switchboard-Provider": self.provider_id})

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        self._record("responses_stream")
        try:
            async for chunk in self.client.stream_response(body):
                yield chunk
        except CustomAPIError as exc:
            yield _failed_sse(str(exc))
