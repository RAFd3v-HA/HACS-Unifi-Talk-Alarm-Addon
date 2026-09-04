"""Authenticated aiohttp implementation of the sidecar API v1 contract."""

from __future__ import annotations

import hmac
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import AppConfig
from .errors import ServiceError
from .manager import CallManager

_LOGGER = logging.getLogger(__name__)
_NUMBER = re.compile(r"^\+?[0-9*#]{1,31}$")
_CALL_ID = re.compile(r"^[0-9A-Za-z._:-]{1,128}$")


def _error_response(error: ServiceError) -> web.Response:
    headers = {"Retry-After": "60"} if error.code == "rate_limited" else None
    return web.json_response(
        {"error": {"code": error.code, "message": error.message}},
        status=error.status,
        headers=headers,
    )


@web.middleware
async def error_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ServiceError as err:
        return _error_response(err)
    except web.HTTPException as err:
        return _error_response(
            ServiceError(err.status, "http_error", "The requested API resource is unavailable")
        )
    except Exception:
        _LOGGER.exception("Unhandled sidecar API failure")
        return _error_response(
            ServiceError(500, "internal_error", "The sidecar could not process the request")
        )


@web.middleware
async def auth_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    expected: str = request.app["config"].api_token
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    valid_shape = bool(supplied) and len(supplied) <= 512 and not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in supplied
    )
    valid_token = hmac.compare_digest(supplied if valid_shape else "", expected)
    if not (valid_shape and valid_token):
        raise ServiceError(401, "unauthorized", "A valid bearer token is required")
    return await handler(request)


async def _json_object(
    request: web.Request, *, allowed_keys: frozenset[str]
) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise ServiceError(400, "invalid_json", "A JSON request body is required")
    try:
        payload = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ServiceError(400, "invalid_json", "The request body is not valid JSON") from err
    if not isinstance(payload, dict):
        raise ServiceError(422, "invalid_request", "The JSON body must be an object")
    unknown = set(payload) - allowed_keys
    if unknown:
        raise ServiceError(422, "unknown_field", "The request contains an unknown field")
    return payload


def _number(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError(422, "invalid_number", "number must be a string")
    normalized = re.sub(r"[\s().-]", "", value.strip())
    if not normalized or len(normalized) > 32 or not _NUMBER.fullmatch(normalized):
        raise ServiceError(
            422,
            "invalid_number",
            "number must contain only a leading plus, digits, star, or hash",
        )
    return normalized


def _message(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError(422, "invalid_message", "message must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ServiceError(422, "invalid_message", "message is empty, too long, or unsafe")
    return normalized


def _audio_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError(422, "invalid_audio_url", "audio_url must be a string")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        not normalized
        or len(normalized) > 2048
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ServiceError(422, "invalid_audio_url", "audio_url is not a safe HTTP URL")
    try:
        parsed.port
    except ValueError as err:
        raise ServiceError(422, "invalid_audio_url", "audio_url has an invalid port") from err
    return normalized


def _ring_timeout(value: Any) -> int:
    if isinstance(value, bool):
        raise ServiceError(422, "invalid_ring_timeout", "ring_timeout must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as err:
        raise ServiceError(422, "invalid_ring_timeout", "ring_timeout must be an integer") from err
    if result < 5 or result > 120:
        raise ServiceError(422, "invalid_ring_timeout", "ring_timeout must be from 5 to 120")
    return result


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "api_version": "1",
            "service": {
                "name": "unifi-talk-alarm-sidecar",
                "version": __version__,
            },
        }
    )


async def status(request: web.Request) -> web.Response:
    registration, call = await request.app["manager"].status()
    return web.json_response(
        {"registration": registration.to_dict(), "call": call.to_dict()}
    )


async def create_call(request: web.Request) -> web.Response:
    payload = await _json_object(
        request,
        allowed_keys=frozenset({"number", "message", "audio_url", "ring_timeout"}),
    )
    has_message = "message" in payload and payload.get("message") is not None
    has_url = "audio_url" in payload and payload.get("audio_url") is not None
    if has_message == has_url:
        raise ServiceError(
            422,
            "invalid_audio_source",
            "Exactly one of message and audio_url is required",
        )
    call = await request.app["manager"].start_call(
        number=_number(payload.get("number")),
        message=_message(payload["message"]) if has_message else None,
        audio_url=_audio_url(payload["audio_url"]) if has_url else None,
        ring_timeout=_ring_timeout(payload.get("ring_timeout", 30)),
    )
    return web.json_response(
        {"call_id": call.call_id, "state": call.state}, status=202
    )


async def hangup_call(request: web.Request) -> web.Response:
    await _json_object(request, allowed_keys=frozenset())
    call_id = request.match_info.get("call_id", "")
    if not _CALL_ID.fullmatch(call_id):
        raise ServiceError(404, "call_not_found", "Unknown or expired call ID")
    call = await request.app["manager"].hangup(call_id)
    return web.json_response({"call_id": call.call_id, "state": call.state}, status=202)


async def refresh(request: web.Request) -> web.Response:
    await _json_object(request, allowed_keys=frozenset())
    await request.app["manager"].refresh()
    return web.json_response({"accepted": True}, status=202)


def create_app(config: AppConfig, manager: CallManager) -> web.Application:
    """Build the API app with a small request-body limit."""
    app = web.Application(
        middlewares=(error_middleware, auth_middleware), client_max_size=32 * 1024
    )
    app["config"] = config
    app["manager"] = manager
    app.router.add_get("/api/v1/health", health)
    app.router.add_get("/api/v1/status", status)
    app.router.add_post("/api/v1/calls", create_call)
    app.router.add_post("/api/v1/calls/{call_id}/hangup", hangup_call)
    app.router.add_post("/api/v1/refresh", refresh)
    return app

