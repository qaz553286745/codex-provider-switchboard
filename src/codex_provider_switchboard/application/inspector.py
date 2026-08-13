from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class RequestInspector:
    """Keep only content-free metadata for the local dashboard."""

    _ALLOWED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "provider",
            "action",
            "model",
            "effort",
            "session_reused",
        }
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None

    def record(self, **values: Any) -> None:
        safe = {
            key: copy.deepcopy(value)
            for key, value in values.items()
            if key in self._ALLOWED_FIELDS
        }
        with self._lock:
            self._last = {"at": int(time.time()), **safe}
        logger.info(
            "Upstream action provider=%s action=%s effort=%s session_reused=%s",
            safe.get("provider", "unknown"),
            safe.get("action", "unknown"),
            safe.get("effort", "auto"),
            bool(safe.get("session_reused")),
        )

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._last)
