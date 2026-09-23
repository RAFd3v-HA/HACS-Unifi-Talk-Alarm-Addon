"""Deterministic fake SIP and media implementations."""

from __future__ import annotations

from pathlib import Path

from talk_alarm.adapters import AdapterEvent, AdapterEventType, EventHandler, SipAdapter
from talk_alarm.audio import PreparedAudio


class FakeSipAdapter(SipAdapter):
    def __init__(self) -> None:
        self.handler: EventHandler | None = None
        self.dialed: list[str] = []
        self.audio_started: list[Path] = []
        self.hangups = 0
        self.audio_cleanups = 0
        self.refreshes = 0
        self.started = False

    def set_event_handler(self, handler: EventHandler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def dial(self, number: str) -> None:
        self.dialed.append(number)

    async def hangup(self) -> None:
        self.hangups += 1

    async def start_audio(self, path: Path) -> None:
        self.audio_started.append(path)

    async def cleanup_call_audio(self) -> None:
        self.audio_cleanups += 1

    async def refresh(self) -> None:
        self.refreshes += 1

    async def emit(self, event: AdapterEventType, reason: str | None = None) -> None:
        assert self.handler is not None
        await self.handler(AdapterEvent(event, reason))


class FakeAudioProvider:
    def __init__(self, root: Path, duration: float = 0.05) -> None:
        self.root = root
        self.duration = duration
        self.messages: list[str] = []
        self.urls: list[str] = []
        self.wav_payloads: list[bytes] = []
        self._counter = 0

    def _audio(self) -> PreparedAudio:
        self._counter += 1
        directory = self.root / f"audio-{self._counter}"
        directory.mkdir()
        path = directory / "alarm.wav"
        path.write_bytes(b"fake")
        return PreparedAudio(path=path, duration=self.duration, directory=directory)

    async def from_message(self, message: str) -> PreparedAudio:
        self.messages.append(message)
        return self._audio()

    async def from_url(self, audio_url: str) -> PreparedAudio:
        self.urls.append(audio_url)
        return self._audio()

    async def from_wav_bytes(self, audio_wav: bytes) -> PreparedAudio:
        self.wav_payloads.append(audio_wav)
        return self._audio()
