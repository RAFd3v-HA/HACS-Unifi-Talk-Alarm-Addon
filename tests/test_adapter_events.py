"""Tests for baresip ctrl_tcp events and privacy-safe call diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from talk_alarm.adapters import (
    AdapterEventType,
    BaresipCtrlTcpAdapter,
    _audio_error_cause,
    _close_cause,
)
from talk_alarm.config import AppConfig
from talk_alarm.manager import CallManager

from fakes import FakeAudioProvider


async def test_baresip_call_and_registration_events_are_parsed(
    app_config: AppConfig,
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)
    fixtures = (
        {"event": True, "class": "register", "type": "REGISTER_OK", "param": ""},
        {"event": True, "class": "call", "type": "CALL_INCOMING", "param": ""},
        {"event": True, "class": "call", "type": "CALL_RINGING", "param": ""},
        {"event": True, "class": "call", "type": "CALL_ESTABLISHED", "param": ""},
        {"event": True, "class": "call", "type": "CALL_CLOSED", "param": "486 Busy Here"},
    )
    for fixture in fixtures:
        await adapter._handle_ctrl_message(fixture)
    await adapter.wait_for_queued_events()

    assert [event.type for event in events] == [
        AdapterEventType.REGISTERED,
        AdapterEventType.CALL_INCOMING,
        AdapterEventType.CALL_RINGING,
        AdapterEventType.CALL_ESTABLISHED,
        AdapterEventType.CALL_CLOSED,
    ]
    assert events[-1].reason == "busy"
    if adapter._event_task is not None:
        adapter._event_task.cancel()


async def test_call_diagnostics_log_only_event_names_and_sip_code(
    app_config: AppConfig, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)
    secret = "private-sip-password-and-token"
    fixtures = (
        {"event": True, "class": "call", "type": "CALL_RINGING", "param": secret},
        {"event": True, "class": "call", "type": "CALL_ANSWERED", "param": secret},
        {"event": True, "class": "other", "type": "CALL_RTPESTAB", "param": secret},
        {"event": True, "class": "other", "type": "CALL_REMOTE_SDP", "param": secret},
        {"event": True, "class": "other", "type": "AUDIO_ERROR", "param": secret},
        {
            "event": True,
            "class": "call",
            "type": "CALL_CLOSED",
            "param": f"486 Busy Here {secret}",
        },
    )
    try:
        with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
            for fixture in fixtures:
                await adapter._handle_ctrl_message(
                    {
                        **fixture,
                        "peeruri": "sip:+491555123456@private.invalid",
                        "accountaor": f"sip:{secret}@private.invalid",
                    }
                )
            await adapter.wait_for_queued_events()

        assert [event.type for event in events] == [
            AdapterEventType.CALL_RINGING,
            AdapterEventType.CALL_CLOSED,
        ]
        assert events[-1].reason == "busy"
        assert [record.getMessage() for record in caplog.records] == [
            "Baresip call event CALL_RINGING",
            "Baresip call event CALL_ANSWERED",
            "Baresip media event CALL_RTPESTAB",
            "Baresip media event CALL_REMOTE_SDP kind=unknown",
            "Baresip media event AUDIO_ERROR cause=unclassified",
            "Baresip call event CALL_CLOSED sip_code=486 cause=sip_response",
        ]
        assert secret not in caplog.text
        assert "+491555123456" not in caplog.text
        assert "private.invalid" not in caplog.text
    finally:
        if adapter._event_task is not None:
            adapter._event_task.cancel()


async def test_closed_param_cannot_become_established_event(
    app_config: AppConfig, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)
    try:
        with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
            await adapter._handle_ctrl_message(
                {
                    "event": True,
                    "class": "call",
                    "type": "CALL_CLOSED",
                    "param": "Connection reset CALL_ESTABLISHED token=private-value",
                }
            )
            await adapter.wait_for_queued_events()

        assert [event.type for event in events] == [AdapterEventType.CALL_CLOSED]
        assert [record.getMessage() for record in caplog.records] == [
            "Baresip call event CALL_CLOSED sip_code=unknown cause=unclassified"
        ]
        assert "private-value" not in caplog.text
    finally:
        if adapter._event_task is not None:
            adapter._event_task.cancel()


@pytest.mark.parametrize(
    ("detail", "expected"),
    (
        ("", "unspecified"),
        ("Connection reset by peer", "connection_reset"),
        ("end of file", "audio_eof"),
        ("Local timeout", "local_timeout"),
        ("No audio codecs", "codec_mismatch"),
        ("rtp stream error", "rtp_stream_error"),
        ("mediaenc failed", "media_encryption_failed"),
        ("mediaenc failed I/O error", "media_encryption_failed"),
        ("Wrong address family", "address_family_error"),
        ("No such file or directory", "file_not_found"),
        ("486 Busy Here", "sip_response"),
        ("Protocol error sip:+491555123456@private.invalid", "unclassified"),
    ),
)
def test_close_reason_is_only_a_fixed_category(detail: str, expected: str) -> None:
    assert _close_cause(detail) == expected


@pytest.mark.parametrize(
    ("detail", "expected"),
    (
        ("0,end of file", "audio_eof"),
        ("5,source failed", "nonzero_audio_error"),
        ("0,private-sip-password-and-token", "unclassified"),
    ),
)
def test_audio_error_is_only_a_fixed_category(detail: str, expected: str) -> None:
    assert _audio_error_cause(detail) == expected


def test_process_warning_diagnostics_never_log_raw_output(
    app_config: AppConfig, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    secret = "sip:+491555123456@private.invalid token=private-password"
    with caplog.at_level(logging.WARNING, logger="talk_alarm.adapters"):
        adapter._log_process_diagnostic(
            f"call: could not decode SDP answer: Protocol error {secret}"
        )
        adapter._log_process_diagnostic(
            f"Warning: call: update: audio_decoder_set error: {secret}"
        )
        adapter._log_process_diagnostic(
            f"call: secure: could not start audio: {secret}"
        )
        adapter._log_process_diagnostic(
            f"call: mnatconn: could not start audio: {secret}"
        )
        adapter._log_process_diagnostic(
            f"aufile: failed to open file '{secret}': No such file or directory"
        )
        adapter._log_process_diagnostic(f"call: unrelated warning {secret}")

    assert [record.getMessage() for record in caplog.records] == [
        "Baresip diagnostic cause=sdp_answer_decode_failed",
        "Baresip diagnostic cause=audio_decoder_failed",
        "Baresip diagnostic cause=audio_start_failed",
        "Baresip diagnostic cause=audio_start_failed",
        "Baresip diagnostic cause=audio_source_file_open_failed",
    ]
    assert secret not in caplog.text


async def test_answered_then_closed_without_established_is_diagnosed(
    app_config: AppConfig, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)
    try:
        with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
            await adapter._handle_ctrl_message(
                {"event": True, "class": "call", "type": "CALL_ANSWERED", "param": ""}
            )
            await adapter._handle_ctrl_message(
                {
                    "event": True,
                    "class": "other",
                    "type": "CALL_REMOTE_SDP",
                    "param": "answer",
                }
            )
            await adapter._handle_ctrl_message(
                {"event": True, "class": "call", "type": "CALL_CLOSED", "param": ""}
            )
            await adapter.wait_for_queued_events()

        assert [event.type for event in events] == [AdapterEventType.CALL_CLOSED]
        assert [record.getMessage() for record in caplog.records] == [
            "Baresip call event CALL_ANSWERED",
            "Baresip media event CALL_REMOTE_SDP kind=answer",
            "Baresip call event CALL_CLOSED sip_code=unknown cause=unspecified",
        ]
    finally:
        if adapter._event_task is not None:
            adapter._event_task.cancel()


async def test_aufile_preload_is_not_mistaken_for_playback_eof(
    app_config: AppConfig,
) -> None:
    """Loading a WAV to memory must not hang up before playback starts."""
    adapter = BaresipCtrlTcpAdapter(app_config)
    events = []

    async def collect(event):
        events.append(event)

    adapter.set_event_handler(collect)

    await adapter._parse_line("aufile: read end of file\n")
    assert events == []

    await adapter._parse_line("aufile: end of file\n")
    assert [event.type for event in events] == [AdapterEventType.AUDIO_EOF]


async def test_aufile_preload_keeps_connected_call_alive(
    app_config: AppConfig,
) -> None:
    """The source preload marker must not hang up an established call."""
    adapter = BaresipCtrlTcpAdapter(app_config)
    commands: list[tuple[str, str]] = []

    async def command(name: str, params: str = "") -> dict:
        commands.append((name, params))
        return {"response": True, "ok": True, "data": ""}

    adapter._command = command
    object.__setattr__(app_config, "baresip_config_dir", Path("/tmp/talk-test"))
    short_audio_root = TemporaryDirectory(prefix="talk-test-", dir="/tmp")
    manager = CallManager(
        app_config, adapter, FakeAudioProvider(Path(short_audio_root.name), duration=60.0)
    )
    try:
        await manager.start_call(
            number="150", message="Alarm", audio_url=None, ring_timeout=30
        )
        await adapter._parse_line("CALL_ESTABLISHED\n")
        _, call = await manager.status()
        assert call.state == "connected"

        await adapter._parse_line("aufile: read end of file\n")
        _, call = await manager.status()
        assert call.state == "connected"
        assert [name for name, _ in commands].count("hangup") == 0

        await adapter._parse_line("aufile: end of file\n")
        _, call = await manager.status()
        assert call.state == "ending"
        assert [name for name, _ in commands].count("hangup") == 1
    finally:
        await manager.shutdown()
        short_audio_root.cleanup()


async def test_generated_baresip_configuration_is_single_call_and_ephemeral(
    app_config: AppConfig,
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    adapter._write_configuration()

    generated = (app_config.baresip_config_dir / "config").read_text(encoding="utf-8")
    generated_lines = generated.splitlines()
    assert "module_path /usr/lib/baresip/modules" in generated_lines
    assert generated_lines.index("module_path /usr/lib/baresip/modules") < next(
        index
        for index, line in enumerate(generated_lines)
        if line.startswith(("module ", "module_tmp ", "module_app "))
    )
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


async def test_each_dial_resets_stale_audio_source_before_call(
    app_config: AppConfig,
) -> None:
    """A previous call's temporary WAV must not become the next call's source."""
    adapter = BaresipCtrlTcpAdapter(app_config)
    commands: list[tuple[str, str]] = []

    async def command(name: str, params: str = "") -> dict:
        commands.append((name, params))
        return {"response": True, "ok": True, "data": ""}

    adapter._command = command
    object.__setattr__(app_config, "baresip_config_dir", Path("/tmp/talk-test"))
    first_audio = Path("/tmp/first.wav")
    second_audio = Path("/tmp/second.wav")

    await adapter.dial("150")
    await adapter.start_audio(first_audio)
    await adapter.dial("151")
    await adapter.start_audio(second_audio)

    silence = f"aufile,{(app_config.baresip_config_dir / 'silence.wav').resolve()}"
    assert commands == [
        ("ausrc", silence),
        ("dial", "sip:150@192.168.1.1:5060"),
        ("ausrc", f"aufile,{first_audio.resolve()}"),
        ("ausrc", silence),
        ("dial", "sip:151@192.168.1.1:5060"),
        ("ausrc", f"aufile,{second_audio.resolve()}"),
    ]


@pytest.mark.parametrize(
    "response",
    (
        {"ok": False, "data": ""},
        {"ok": True, "data": "failed to set audio-source (private-reason)"},
        {"ok": True, "data": ""},
    ),
)
async def test_failed_silence_reset_never_dials(
    app_config: AppConfig, response: dict, caplog
) -> None:
    """Even an ok=true ctrl response may report a failed source switch."""
    adapter = BaresipCtrlTcpAdapter(app_config)
    object.__setattr__(app_config, "baresip_config_dir", Path("/tmp/talk-test"))
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
                    {"response": True, "token": payload["token"], **response}
                )
            )

        async def drain(self) -> None:
            await asyncio.sleep(0)

    adapter._ctrl_writer = FakeWriter()
    with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
        with pytest.raises(RuntimeError):
            await adapter.dial("150")

    assert [frame["command"] for frame in frames] == ["ausrc"]
    assert frames[0]["params"] == (
        f"aufile,{(app_config.baresip_config_dir / 'silence.wav').resolve()}"
    )
    assert "private-reason" not in caplog.text


async def test_long_audio_source_path_is_rejected_before_ctrl_command(
    app_config: AppConfig, tmp_path: Path, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    commands: list[tuple[str, str]] = []

    async def command(name: str, params: str = "") -> dict:
        commands.append((name, params))
        return {"response": True, "ok": True, "data": ""}

    adapter._command = command
    private_path = tmp_path / ("private-password-" + "x" * 128 + ".wav")
    with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
        with pytest.raises(RuntimeError):
            await adapter.start_audio(private_path)

    assert commands == []
    assert "private-password" not in caplog.text


async def test_event_handler_command_does_not_block_ctrl_response(
    app_config: AppConfig, caplog
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
                        "data": (
                            "switch audio device: "
                            "aufile,/tmp/private-command-response.wav"
                        ),
                    }
                )
            )

        async def drain(self) -> None:
            await asyncio.sleep(0)

    adapter._ctrl_writer = FakeWriter()

    async def on_event(event):
        if event.type == AdapterEventType.CALL_ESTABLISHED:
            await adapter.start_audio(Path("/tmp/alarm.wav"))

    adapter.set_event_handler(on_event)
    with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
        await adapter._handle_ctrl_message(
            {"event": True, "class": "call", "type": "CALL_ESTABLISHED", "param": ""}
        )
        await asyncio.wait_for(adapter.wait_for_queued_events(), timeout=1)
    assert frames[0]["command"] == "ausrc"
    assert frames[0]["params"].startswith("aufile,")
    assert "Baresip command ausrc accepted" in caplog.text
    assert "private-command-response" not in caplog.text
    if adapter._event_task is not None:
        adapter._event_task.cancel()


async def test_ausrc_response_failure_is_logged_without_path_or_reason(
    app_config: AppConfig, caplog
) -> None:
    adapter = BaresipCtrlTcpAdapter(app_config)
    private_path = Path("/tmp/private-password.wav")

    class FakeWriter:
        def is_closing(self) -> bool:
            return False

        def write(self, frame: bytes) -> None:
            colon = frame.index(b":")
            length = int(frame[:colon])
            payload = json.loads(frame[colon + 1 : colon + 1 + length])
            asyncio.create_task(
                adapter._handle_ctrl_message(
                    {
                        "response": True,
                        "ok": True,
                        "token": payload["token"],
                        "data": (
                            f"switch audio device: aufile,{private_path}\n"
                            "failed to set audio-source (private-reason)\n"
                        ),
                    }
                )
            )

        async def drain(self) -> None:
            await asyncio.sleep(0)

    adapter._ctrl_writer = FakeWriter()
    with caplog.at_level(logging.INFO, logger="talk_alarm.adapters"):
        with pytest.raises(RuntimeError):
            await adapter.start_audio(private_path)

    assert [record.getMessage() for record in caplog.records] == [
        "Baresip command ausrc source_switch_failed"
    ]
    assert str(private_path) not in caplog.text
    assert "private-reason" not in caplog.text
