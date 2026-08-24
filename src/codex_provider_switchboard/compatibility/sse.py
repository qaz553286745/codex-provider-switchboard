from __future__ import annotations

import json
import re
from typing import Any


class ResponsesSSEError(ValueError):
    """An upstream Responses stream violated the SSE/JSON contract."""


class ResponsesSSEDecoder:
    _separator = re.compile(rb"\r?\n\r?\n")

    def __init__(self, *, event_limit: int) -> None:
        self.event_limit = event_limit
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.buffer.extend(chunk)
        if len(self.buffer) > self.event_limit:
            raise ResponsesSSEError("Upstream SSE event exceeded the byte limit.")
        result: list[dict[str, Any]] = []
        while match := self._separator.search(self.buffer):
            block = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            event = self._decode(block)
            if event is not None:
                result.append(event)
        return result

    @staticmethod
    def _decode(block: bytes) -> dict[str, Any] | None:
        try:
            lines = block.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ResponsesSSEError("Upstream SSE was not valid UTF-8.") from exc
        data: list[str] = []
        for line in lines:
            if line.startswith("data:"):
                part = line[5:]
                data.append(part[1:] if part.startswith(" ") else part)
        if not data or data == ["[DONE]"]:
            return None
        try:
            value = json.loads("\n".join(data))
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ResponsesSSEError("Upstream SSE event was not valid JSON.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ResponsesSSEError("Upstream SSE event had an invalid shape.")
        return value

    def finish(self) -> None:
        if self.buffer.strip():
            raise ResponsesSSEError("Upstream SSE ended with an incomplete event.")


def encode_response_event(event: dict[str, Any]) -> bytes:
    event_type = str(event.get("type") or "response.event")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode()
