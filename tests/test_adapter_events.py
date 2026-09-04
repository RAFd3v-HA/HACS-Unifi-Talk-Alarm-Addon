"""Tests for current baresip ctrl_tcp JSON event shapes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from talk_alarm.adapters import AdapterEventType, BaresipCtrlTcpAdapter
from talk_alarm.config import AppConfig


async def test_official_ctrl_tcp_event_objects_are_parsed(app_config: AppConfig) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)
    fixtures = (
        {"event": True, "class": "ua", "type": "REGISTER_OK", "param": ""},
        {"event": True, "class": "call", "type": "CALL_INCOMING", "param": ""},
        {"event": True, "class": "call", "type": "CALL_OUTGOING", "param": ""},
        {"event": True, "class": "call", "type": "CALL_RINGING", "param": ""},
        {"event": True, "class": "call", "type": "CALL_ESTABLISHED", "param": ""},
        {"event": True, "class": "audio", "type": "END_OF_FILE", "param": "aufile"},
        {"event": True, "class": "call", "type": "CALL_CLOSED", "param": "486 Busy Here"},
    )
    for fixture in fixtures:
        await adapter._handle_ctrl_message(fixture)
    await adapter.wait_for_queued_events()

    assert [event.type for event in events] == [
        AdapterEventType.REGISTERED,
        AdapterEventType.CALL_INCOMING,
        AdapterEventType.CALL_DIALING,
        AdapterEventType.CALL_RINGING,
        AdapterEventType.CALL_ESTABLISHED,
        AdapterEventType.AUDIO_EOF,
        AdapterEventType.CALL_CLOSED,
    ]
    assert events[-1].reason == "busy"
    if adapter._event_task is not None:
        adapter._event_task.cancel()


async def test_generated_baresip_configuration_is_single_call_and_ephemeral(
    app_config: AppConfig,
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    adapter._write_configuration()

    generated = (app_config.baresip_config_dir / "config").read_text(encoding="utf-8")
    assert "call_local_timeout 5" in generated
    assert "call_max_calls 1" in generated
    assert "ausrc_channels 1" in generated
    assert "auplay_channels 1" in generated
    assert "audio_channels" not in generated
    assert "module_tmp account.so" in generated
    assert "module_app menu.so" in generated
    assert "module_app ctrl_tcp.so" in generated
    assert f"audio_player aufile,{app_config.runtime_dir / 'incoming.wav'}" in generated
    assert str(app_config.baresip_config_dir / "incoming.wav") not in generated
    assert "ctrl_tcp_listen 127.0.0.1:4444" in generated


async def test_received_audio_cleanup_removes_temporary_file(
    app_config: AppConfig,
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    app_config.runtime_dir.mkdir(parents=True)
    incoming = app_config.runtime_dir / "incoming.wav"
    incoming.write_bytes(b"received speech")

    await adapter.cleanup_call_audio()

    assert not incoming.exists()


async def test_event_handler_command_does_not_block_ctrl_response(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """CALL_ESTABLISHED may issue ausrc while the ctrl reader stays available."""
    adapter = BaresipCtrlTcpAdapter(app_config)
    frames: list[dict] = []

    class FakeWriter:
        def is_closing(self) -> bool:
            return False

        def write(self, frame: bytes) -> None:
            colon = frame.index(b":")
            length = int(frame[:colon])
            payload = json.loads(frame[colon + 1 : colon + 1 + length])
            frames.append(payload)
            asyncio.create_task(
                adapter._handle_ctrl_message(
                    {
                        "response": True,
                        "ok": True,
                        "token": payload["token"],
                        "data": "ok",
                    }
                )
            )

        async def drain(self) -> None:
            await asyncio.sleep(0)

    adapter._ctrl_writer = FakeWriter()

    async def on_event(event):
        if event.type == AdapterEventType.CALL_ESTABLISHED:
            await adapter.start_audio(tmp_path / "alarm.wav")

    adapter.set_event_handler(on_event)
    await adapter._handle_ctrl_message(
        {"event": True, "class": "call", "type": "CALL_ESTABLISHED", "param": ""}
    )
    await asyncio.wait_for(adapter.wait_for_queued_events(), timeout=1)
    assert frames[0]["command"] == "ausrc"
    assert frames[0]["params"].startswith("aufile,")
    if adapter._event_task is not None:
        adapter._event_task.cancel()
