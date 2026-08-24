from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import zstandard
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..compatibility.responses import bind_transport_context
from ..infrastructure.codex_config import CodexConfigError
from ..providers.base import ProviderError
from ..runtime import Runtime, build_runtime
from .responses_transport import (
    PayloadError,
    guarded_responses_sse,
    run_responses_websocket,
    validate_responses_body,
)

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
SSE_HEARTBEAT_SECONDS = 15.0


def _is_loopback(value: str | None) -> bool:
    if value is None:
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _error(status: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": None,
            }
        },
    )


async def _json_body(request: Request, limit: int) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() not in {"application/json", "text/json"}:
        raise PayloadError("Content-Type must be application/json.", status_code=415)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError as exc:
            raise PayloadError("Invalid Content-Length header.") from exc
        if parsed_length > limit:
            raise PayloadError("Request body is too large.", status_code=413)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise PayloadError("Request body is too large.", status_code=413)
    decoded = _decode_content_encoding(
        bytes(raw),
        request.headers.get("content-encoding"),
        limit,
    )

    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        payload = json.loads(decoded, parse_constant=reject_constant)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise PayloadError("Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PayloadError("Request body must be a JSON object.")
    return payload


def _decode_zlib_body(raw: bytes, limit: int, *, wbits: int) -> bytes:
    decompressor = zlib.decompressobj(wbits)
    try:
        decoded = decompressor.decompress(raw, limit + 1)
        if len(decoded) > limit or decompressor.unconsumed_tail:
            raise PayloadError(
                "Decompressed request body is too large.", status_code=413
            )
        remaining = limit + 1 - len(decoded)
        decoded += decompressor.flush(remaining)
    except zlib.error as exc:
        raise PayloadError("Request body compression is invalid.") from exc
    if len(decoded) > limit:
        raise PayloadError("Decompressed request body is too large.", status_code=413)
    if not decompressor.eof or decompressor.unused_data:
        raise PayloadError("Request body compression is invalid.")
    return decoded


def _decode_content_encoding(
    raw: bytes,
    content_encoding: str | None,
    limit: int,
) -> bytes:
    encodings = [
        item.strip().lower()
        for item in (content_encoding or "identity").split(",")
        if item.strip()
    ]
    if not encodings:
        encodings = ["identity"]
    if len(encodings) != 1:
        raise PayloadError(
            "Multiple Content-Encoding values are not supported.", status_code=415
        )
    encoding = encodings[0]
    if encoding == "identity":
        return raw
    if encoding in {"gzip", "x-gzip"}:
        return _decode_zlib_body(raw, limit, wbits=zlib.MAX_WBITS | 16)
    if encoding == "deflate":
        try:
            return _decode_zlib_body(raw, limit, wbits=zlib.MAX_WBITS)
        except PayloadError as exc:
            if exc.status_code == 413:
                raise
        return _decode_zlib_body(raw, limit, wbits=-zlib.MAX_WBITS)
    if encoding == "zstd":
        try:
            parameters = zstandard.get_frame_parameters(raw)
            if (
                parameters.content_size != zstandard.CONTENTSIZE_UNKNOWN
                and parameters.content_size > limit
            ) or parameters.window_size > max(1_024, limit):
                raise PayloadError(
                    "Decompressed request body is too large.", status_code=413
                )
            decompressor = zstandard.ZstdDecompressor(
                max_window_size=max(1_024, limit)
            ).decompressobj()
            decoded = bytearray()
            for start in range(0, len(raw), 64):
                decoded.extend(decompressor.decompress(raw[start : start + 64]))
                if len(decoded) > limit:
                    raise PayloadError(
                        "Decompressed request body is too large.",
                        status_code=413,
                    )
            decoded.extend(decompressor.flush())
        except zstandard.ZstdError as exc:
            raise PayloadError("Request body compression is invalid.") from exc
        if len(decoded) > limit:
            raise PayloadError(
                "Decompressed request body is too large.", status_code=413
            )
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise PayloadError("Request body compression is invalid.")
        return bytes(decoded)
    raise PayloadError(f"Unsupported Content-Encoding: {encoding}.", status_code=415)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    resolved = runtime or build_runtime()
    settings = resolved.settings
    service = resolved.service

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resolved.oauth.close()

    app = FastAPI(
        title="Codex Provider Switchboard",
        version=__version__,
        description=(
            "A local OpenAI Responses-compatible bridge for Kiro CLI and "
            "Cursor Agent CLI or Cloud Agents, native direct providers, and "
            "user-configured Responses APIs."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = resolved
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def host_allowed(request: Request | WebSocket) -> bool:
        if not _is_loopback(settings.host):
            return True
        return _is_loopback(request.url.hostname)

    def authorized(request: Request | WebSocket) -> bool:
        if not host_allowed(request):
            return False
        if settings.token is None:
            return True
        scheme, separator, supplied = request.headers.get(
            "authorization", ""
        ).partition(" ")
        return (
            bool(separator)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(supplied, settings.token)
        )

    def control_allowed(request: Request) -> bool:
        if not authorized(request):
            return False
        origin = request.headers.get("origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or parsed.path not in {"", "/"}:
            return False
        try:
            expected_port = request.url.port
            origin_port = parsed.port
        except ValueError:
            return False
        if expected_port is None:
            expected_port = 443 if request.url.scheme == "https" else 80
        if origin_port is None:
            origin_port = 443 if parsed.scheme == "https" else 80
        return (
            parsed.scheme == request.url.scheme
            and parsed.hostname == request.url.hostname
            and origin_port == expected_port
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.url.path == "/" or request.url.path.startswith(
            ("/api/", "/v1/", "/health")
        ):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled request failure: %s", type(exc).__name__)
        return _error(500, "Internal server error.", "server_error")

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/control/state")
    async def control_state(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        return JSONResponse(service.control_state())

    @app.put("/api/control/settings")
    async def update_control_settings(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            state = service.update_settings(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(state)

    @app.get("/api/control/cursor/models")
    async def cursor_models(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        force = request.query_params.get("refresh") in {"1", "true"}
        try:
            models = await service.cursor_models(force=force)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse({"models": models})

    @app.get("/api/control/kiro/models")
    async def kiro_models(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        force = request.query_params.get("refresh") in {"1", "true"}
        try:
            models = await service.kiro_models(force=force)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse({"models": models})

    @app.get("/api/control/custom/models")
    async def custom_models(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        force = request.query_params.get("refresh") in {"1", "true"}
        try:
            models = await service.custom_models(force=force)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse({"models": models})

    @app.get("/api/control/direct/platforms")
    async def direct_platforms(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        return JSONResponse({"platforms": service.direct_platforms()})

    @app.get("/api/control/imports/pi")
    async def preview_pi_credentials(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        try:
            preview = service.preview_pi_credentials()
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(preview)

    @app.post("/api/control/imports/pi")
    async def import_pi_credentials(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            result = await service.import_pi_credentials(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except ValueError as exc:
            return _error(400, str(exc), "invalid_request_error")
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(result)

    @app.get("/api/control/direct/models")
    async def direct_models(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        platform_id = request.query_params.get("platform_id")
        force = request.query_params.get("refresh") in {"1", "true"}
        try:
            models = await service.direct_models(platform_id, force=force)
        except (ProviderError, ValueError) as exc:
            if isinstance(exc, ProviderError):
                return _error(exc.status_code, str(exc), exc.error_type)
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse({"models": models})

    @app.put("/api/control/direct/api-key")
    async def save_direct_api_key(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            state = service.set_direct_api_key(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(state)

    @app.delete("/api/control/direct/auth/{platform_id}")
    async def logout_direct(platform_id: str, request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            state = service.logout_direct(platform_id)
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(state)

    @app.post("/api/control/direct/auth/{platform_id}/import")
    async def import_direct_credentials(
        platform_id: str, request: Request
    ) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            state = await service.import_direct_credentials(platform_id, payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(state)

    @app.post("/api/control/direct/test")
    async def test_direct(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            if set(payload) != {"platform_id"} or not isinstance(
                payload.get("platform_id"), str
            ):
                raise ValueError("platform_id is required.")
            result = await service.test_direct(payload["platform_id"])
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except ValueError as exc:
            return _error(400, str(exc), "invalid_request_error")
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(result)

    @app.post("/api/control/direct/auth/{platform_id}/login")
    async def start_direct_login(platform_id: str, request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            login = await service.start_direct_login(
                platform_id,
                payload,
                callback_base_url=str(request.base_url).rstrip("/"),
            )
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(login, status_code=202)

    @app.get("/api/control/direct/auth/login/{session_id}")
    async def direct_login_status(session_id: str, request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        try:
            status = service.direct_login_status(session_id)
        except KeyError:
            return _error(404, "OAuth login was not found.", "not_found")
        return JSONResponse(status)

    @app.post("/api/control/direct/auth/login/{session_id}/respond")
    async def respond_direct_login(session_id: str, request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            status = service.respond_direct_login(session_id, payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except KeyError:
            return _error(404, "OAuth login was not found.", "not_found")
        except ValueError as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(status)

    @app.post("/api/control/direct/auth/login/{session_id}/cancel")
    async def cancel_direct_login(session_id: str, request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            status = await service.cancel_direct_login(session_id)
        except KeyError:
            return _error(404, "OAuth login was not found.", "not_found")
        return JSONResponse(status)

    @app.get("/api/control/direct/oauth/callback/{session_id}")
    async def receive_direct_oauth_callback(
        session_id: str, request: Request
    ) -> HTMLResponse:
        if not host_allowed(request):
            return HTMLResponse("OAuth callback rejected.", status_code=403)
        allowed = {"code", "state", "error", "error_description"}
        parameters = {
            key: value[:16_384]
            for key, value in request.query_params.items()
            if key in allowed
        }
        try:
            service.receive_direct_callback(session_id, parameters)
        except (KeyError, ValueError):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<title>Switchboard OAuth</title>"
                "<p>This OAuth callback is no longer active.</p>",
                status_code=400,
            )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<title>Switchboard OAuth</title>"
            "<p>Authentication received. You may close this window.</p>"
        )

    @app.post("/api/control/cursor/test")
    async def test_cursor(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            models = await service.cursor_models(force=True)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse({"ok": True, "models": models})

    @app.post("/api/control/custom/test")
    async def test_custom(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            models = await service.custom_models(force=True)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse({"ok": True, "models": models})

    @app.get("/api/control/{provider_id}/quota")
    async def provider_quota(provider_id: str, request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        force = request.query_params.get("refresh") in {"1", "true"}
        try:
            quota = await service.quota(provider_id, force=force)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(quota)

    @app.get("/api/control/codex-config")
    async def codex_config_status(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Control request was rejected.", "authentication_error")
        return JSONResponse(resolved.codex_config.status())

    @app.post("/api/control/codex-config/enable")
    async def enable_codex_config(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            status = service.enable_codex_config(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except CodexConfigError as exc:
            return _error(exc.status_code, str(exc), "codex_config_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(status)

    @app.post("/api/control/codex-config/disable")
    async def disable_codex_config(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            status = service.disable_codex_config(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except CodexConfigError as exc:
            return _error(exc.status_code, str(exc), "codex_config_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(status)

    @app.post("/api/control/codex-config/agents")
    async def configure_codex_agents(request: Request) -> JSONResponse:
        if not control_allowed(request):
            return _error(403, "Control request was rejected.", "authentication_error")
        try:
            payload = await _json_body(request, settings.max_request_bytes)
            status = service.configure_codex_agents(payload)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except CodexConfigError as exc:
            return _error(exc.status_code, str(exc), "codex_config_error")
        except (ValueError, OSError) as exc:
            return _error(400, str(exc), "invalid_request_error")
        return JSONResponse(status)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Request was rejected.", "authentication_error")
        return JSONResponse(service.health())

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error(401, "Invalid proxy token.", "authentication_error")
        return JSONResponse(service.models())

    @app.post("/v1/responses/compact")
    async def compact_response(request: Request):
        if not authorized(request):
            return _error(401, "Invalid proxy token.", "authentication_error")
        try:
            body = await _json_body(request, settings.max_request_bytes)
            body = bind_transport_context(body, request.headers)
            validate_responses_body(body)
            provider_response = await service.compact(body)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(
            provider_response.body,
            headers=provider_response.headers,
        )

    @app.post("/v1/responses")
    async def create_response(request: Request):
        if not authorized(request):
            return _error(401, "Invalid proxy token.", "authentication_error")
        try:
            body = await _json_body(request, settings.max_request_bytes)
            body = bind_transport_context(body, request.headers)
            validate_responses_body(body)
        except PayloadError as exc:
            return _error(exc.status_code, str(exc), "invalid_request_error")
        if "input" not in body:
            return _error(400, "Missing required field: input", "invalid_request_error")

        try:
            if body.get("stream") is True:
                provider_id, iterator = service.stream(body)
                event_limit = max(
                    settings.max_request_bytes,
                    settings.kiro_max_output_bytes,
                    settings.cursor_max_output_bytes,
                    settings.direct_max_output_bytes,
                )
                return StreamingResponse(
                    guarded_responses_sse(
                        iterator,
                        event_limit=event_limit,
                        heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Switchboard-Provider": provider_id,
                    },
                )
            provider_response = await service.complete(body)
        except ProviderError as exc:
            return _error(exc.status_code, str(exc), exc.error_type)
        return JSONResponse(
            provider_response.body,
            headers=provider_response.headers,
        )

    @app.websocket("/v1/responses")
    async def create_response_websocket(websocket: WebSocket) -> None:
        if not authorized(websocket):
            await websocket.close(code=1008, reason="Request was rejected.")
            return
        await websocket.accept()
        await run_responses_websocket(websocket, service, settings)

    return app
