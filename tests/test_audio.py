"""Audio preparation tests that do not invoke external media tools."""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import wave

import pytest

from talk_alarm.audio import AudioProcessor
from talk_alarm.config import AppConfig
from talk_alarm.errors import ServiceError


def _normalized_wav() -> bytes:
    """Build a short 8 kHz mono 16-bit PCM WAV."""
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


def _streaming_length_wav() -> bytes:
    """Build a valid PCM WAV whose RIFF and data lengths are not finalized."""
    output = bytearray(_normalized_wav())
    output[4:8] = (0xFFFFFFFF).to_bytes(4, "little")
    data_size_offset = output.index(b"data") + 4
    output[data_size_offset : data_size_offset + 4] = (0xFFFFFFFF).to_bytes(
        4, "little"
    )
    return bytes(output)


async def test_wav_bytes_are_written_validated_normalized_and_cleaned(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = AudioProcessor(app_config)

    async def copy_normalized(
        source: Path, output: Path, *, invalid_status: int
    ) -> None:
        assert invalid_status == 422
        shutil.copyfile(source, output)

    monkeypatch.setattr(processor, "_normalize", copy_normalized)
    prepared = await processor.from_wav_bytes(_normalized_wav())
    try:
        assert prepared.path.name == "alarm.wav"
        assert prepared.duration == pytest.approx(0.01)
        assert prepared.path.read_bytes() == _normalized_wav()
    finally:
        directory = prepared.directory
        prepared.cleanup()
    assert not directory.exists()


async def test_streaming_wav_header_uses_actual_pcm_length(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown streaming RIFF lengths must not look like multi-day audio."""
    processor = AudioProcessor(app_config)

    async def copy_normalized(
        source: Path, output: Path, *, invalid_status: int
    ) -> None:
        assert invalid_status == 422
        shutil.copyfile(source, output)

    monkeypatch.setattr(processor, "_normalize", copy_normalized)
    prepared = await processor.from_wav_bytes(_streaming_length_wav())
    try:
        assert prepared.duration == pytest.approx(0.01)
    finally:
        prepared.cleanup()


@pytest.mark.parametrize("payload", (b"", b"not a wav"))
async def test_wav_bytes_reject_empty_or_invalid_input(
    app_config: AppConfig, payload: bytes
) -> None:
    processor = AudioProcessor(app_config)
    with pytest.raises(ServiceError) as error:
        await processor.from_wav_bytes(payload)
    assert error.value.code == "invalid_audio"
