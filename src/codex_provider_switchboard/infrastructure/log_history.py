from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
            r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(r"\b(?:crsr|sk|mr|su8)[_-][A-Za-z0-9._-]{12,}\b", re.I),
        "<credential-redacted>",
    ),
    (
        re.compile(r"CODEX_SWITCHBOARD_BRIDGE_(?:BEGIN|END)_[A-Za-z0-9_-]+"),
        "<bridge-marker-redacted>",
    ),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            re.I,
        ),
        "<identifier-redacted>",
    ),
)


def redact_log_text(value: str) -> str:
    redacted = value.replace("\r", "\\r")
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def configure_log_history(
    *,
    path: Path,
    level: str,
    max_bytes: int,
    backup_count: int,
) -> Path:
    """Persist bounded, redacted process history without request content."""
    resolved = path.expanduser()
    if resolved.is_symlink():
        raise ValueError("Switchboard log path must not be a symlink.")
    parent_existed = resolved.parent.exists()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(resolved.parent, 0o700)

    formatter = _RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = _PrivateRotatingFileHandler(
        resolved,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._switchboard_history = True  # type: ignore[attr-defined]
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler._switchboard_history = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_switchboard_history", False):
            root.removeHandler(handler)
            handler.close()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(getattr(logging, level.upper()))
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        configured = logging.getLogger(logger_name)
        configured.handlers = []
        configured.propagate = True
    return resolved
