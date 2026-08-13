"""ASGI compatibility entry point for ``uvicorn codex_provider_switchboard.app:app``."""

from .web.app import create_app

app = create_app()


__all__ = ["app", "create_app"]
