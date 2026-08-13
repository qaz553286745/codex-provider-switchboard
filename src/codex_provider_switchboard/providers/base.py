from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class ResponsesProvider(Protocol):
    provider_id: str

    async def complete(self, body: dict[str, Any]) -> ProviderResponse: ...

    def stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]: ...

    def model_id(self) -> str: ...
