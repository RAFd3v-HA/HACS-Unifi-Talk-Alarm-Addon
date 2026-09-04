"""HTTP contract and fake SIP lifecycle tests."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from talk_alarm.adapters import AdapterEventType
from talk_alarm.config import AppConfig
from talk_alarm.manager import CallManager
from talk_alarm.server import create_app

from fakes import FakeAudioProvider, FakeSipAdapter


async def _client(
    config: AppConfig, adapter: FakeSipAdapter, audio: FakeAudioProvider
) -> tuple[TestClient, CallManager]:
    manager = CallManager(config, adapter, audio)
    client = TestClient(TestServer(create_app(config, manager)))
    await client.start_server()
    return client, manager


def _headers(
    token: str = "test-token-with-at-least-32-characters",
) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def test_health_requires_constant_bearer_boundary(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    client, manager = await _client(app_config, adapter, FakeAudioProvider(tmp_path))
    try:
        response = await client.get("/api/v1/health")
        assert response.status == 401
        response = await client.get("/api/v1/health", headers=_headers())
        assert response.status == 200
        assert await response.json() == {
            "status": "ok",
            "api_version": "1",
            "service": {
                "name": "unifi-talk-alarm-sidecar",
                "version": "0.1.0",
            },
        }
    finally:
        await manager.shutdown()
        await client.close()


async def test_audio_starts_only_after_established_and_eof_hangs_up(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    audio = FakeAudioProvider(tmp_path)
    client, manager = await _client(app_config, adapter, audio)
    try:
        response = await client.post(
            "/api/v1/calls",
            headers=_headers(),
            json={"number": "150", "message": "Wasseralarm", "ring_timeout": 30},
        )
        assert response.status == 202
        result = await response.json()
        assert result["state"] == "dialing"
        assert adapter.dialed == ["150"]
        assert adapter.audio_started == []
        assert audio.messages == ["Wasseralarm"]

        await adapter.emit(AdapterEventType.CALL_RINGING)
        assert adapter.audio_started == []
        await adapter.emit(AdapterEventType.CALL_ESTABLISHED)
        assert len(adapter.audio_started) == 1
        registration, call = await manager.status()
        assert call.state == "connected"
        assert call.answered_at is not None

        await adapter.emit(AdapterEventType.AUDIO_EOF)
        assert adapter.hangups == 1
        _, call = await manager.status()
        assert call.state == "ending"
        await adapter.emit(AdapterEventType.CALL_CLOSED, "normal")
        _, call = await manager.status()
        assert call.state == "ended"
    finally:
        await manager.shutdown()
        await client.close()


async def test_number_policy_and_sip_uri_bypass_are_rejected(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    client, manager = await _client(app_config, adapter, FakeAudioProvider(tmp_path))
    try:
        for number in ("112", "1-1-0", "150@attacker.invalid", "sip:150"):
            response = await client.post(
                "/api/v1/calls",
                headers=_headers(),
                json={"number": number, "message": "Test", "ring_timeout": 30},
            )
            assert response.status == 422
            payload = await response.json()
            if number in {"112", "1-1-0"}:
                assert payload["error"]["code"] == "number_not_allowed"
        assert adapter.dialed == []
    finally:
        await manager.shutdown()
        await client.close()


async def test_fourth_call_is_rate_limited_before_sip_dial(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    client, manager = await _client(app_config, adapter, FakeAudioProvider(tmp_path))
    try:
        for _ in range(3):
            response = await client.post(
                "/api/v1/calls",
                headers=_headers(),
                json={"number": "150", "message": "Test", "ring_timeout": 30},
            )
            assert response.status == 202
            await adapter.emit(AdapterEventType.CALL_CLOSED, "failed")

        response = await client.post(
            "/api/v1/calls",
            headers=_headers(),
            json={"number": "150", "message": "Test", "ring_timeout": 30},
        )
        assert response.status == 429
        assert (await response.json())["error"]["code"] == "rate_limited"
        assert len(adapter.dialed) == 3
    finally:
        await manager.shutdown()
        await client.close()


async def test_unknown_hangup_returns_contract_error(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    client, manager = await _client(app_config, adapter, FakeAudioProvider(tmp_path))
    try:
        response = await client.post(
            "/api/v1/calls/unknown/hangup", headers=_headers(), json={}
        )
        assert response.status == 404
        assert (await response.json())["error"]["code"] == "call_not_found"
    finally:
        await manager.shutdown()
        await client.close()
