"""Concurrency and outbound-only call-manager safety tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from talk_alarm.adapters import AdapterEventType
from talk_alarm.audio import PreparedAudio
from talk_alarm.config import AppConfig
from talk_alarm.errors import ServiceError
from talk_alarm.manager import CallManager

from fakes import FakeSipAdapter


class BlockingAudioProvider:
    def __init__(self, root: Path) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.root = root

    async def from_message(self, message: str) -> PreparedAudio:
        self.started.set()
        await self.release.wait()
        directory = self.root / "prepared"
        directory.mkdir()
        path = directory / "alarm.wav"
        path.write_bytes(b"fake")
        return PreparedAudio(path=path, duration=1.0, directory=directory)

    async def from_url(self, audio_url: str) -> PreparedAudio:
        return await self.from_message(audio_url)


async def test_hangup_during_media_preparation_never_dials(
    app_config: AppConfig, tmp_path: Path
) -> None:
    adapter = FakeSipAdapter()
    audio = BlockingAudioProvider(tmp_path)
    manager = CallManager(app_config, adapter, audio)
    start_task = asyncio.create_task(
        manager.start_call(
            number="150",
            message="Alarm",
            audio_url=None,
            ring_timeout=30,
        )
    )
    try:
        await asyncio.wait_for(audio.started.wait(), timeout=1)
        _, call = await manager.status()
        assert call.call_id is not None

        ended = await manager.hangup(call.call_id)
        assert ended.state == "ended"
        audio.release.set()

        with pytest.raises(ServiceError) as error:
            await start_task
        assert error.value.code == "call_cancelled"
        assert adapter.dialed == []
        assert adapter.hangups == 0
    finally:
        audio.release.set()
        if not start_task.done():
            start_task.cancel()
        await manager.shutdown()


async def test_incoming_call_is_rejected_without_becoming_api_state(
    app_config: AppConfig,
) -> None:
    adapter = FakeSipAdapter()
    manager = CallManager(app_config, adapter)
    try:
        await adapter.emit(AdapterEventType.CALL_INCOMING)
        _, call = await manager.status()
        assert call.state == "idle"
        assert adapter.hangups == 1
    finally:
        await manager.shutdown()
