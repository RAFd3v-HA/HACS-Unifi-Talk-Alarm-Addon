"""SIP adapter boundary and the production baresip ctrl_tcp implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import json
import logging
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
import wave

from .config import AppConfig

_LOGGER = logging.getLogger(__name__)
_DIAGNOSTIC_CALL_EVENTS = frozenset(
    {
        "CALL_INCOMING",
        "CALL_OUTGOING",
        "CALL_PROGRESS",
        "CALL_RINGING",
        "CALL_ANSWERED",
        "CALL_ESTABLISHED",
        "CALL_CLOSED",
    }
)
_SIP_STATUS = re.compile(r"^\s*(?:SIP/2\.0\s+)?([1-6][0-9]{2})(?=\s|$)")
_CLOSE_CAUSES = {
    "connection reset by peer": "connection_reset",
    "end of file": "audio_eof",
    "local timeout": "local_timeout",
    "mediaenc failed": "media_encryption_failed",
    "no audio codecs": "codec_mismatch",
    "no such file or directory": "file_not_found",
    "rtp stream error": "rtp_stream_error",
    "wrong address family": "address_family_error",
}
_PROCESS_CAUSES = (
    ("call: could not decode sdp answer:", "sdp_answer_decode_failed"),
    ("call: update: audio_decoder_set error:", "audio_decoder_failed"),
    ("call: start: audio_encoder_set error:", "audio_encoder_failed"),
    ("call: start: audio_decoder_set error:", "audio_decoder_failed"),
    ("call: start: audio_start error:", "audio_start_failed"),
    ("call: could not start audio:", "audio_start_failed"),
    ("call: secure: could not start audio:", "audio_start_failed"),
    ("call: mnatconn: could not start audio:", "audio_start_failed"),
    ("audio: alloc decoder:", "audio_decoder_failed"),
    ("audio: alloc encoder:", "audio_encoder_failed"),
    ("audio: start_source failed (", "audio_source_start_failed"),
    ("audio: start_player failed (", "audio_player_start_failed"),
    ("aufile: failed to open file '", "audio_source_file_open_failed"),
)


def _close_cause(detail: str) -> str:
    """Classify a Baresip close reason without exposing its free-form text."""
    if _SIP_STATUS.match(detail):
        return "sip_response"
    normalized = detail.strip().lower()
    if not normalized:
        return "unspecified"
    if normalized.startswith("mediaenc failed "):
        return "media_encryption_failed"
    return _CLOSE_CAUSES.get(normalized, "unclassified")


def _audio_error_cause(detail: str) -> str:
    """Baresip reports normal aufile EOF through its AUDIO_ERROR event."""
    normalized = detail.strip().lower()
    if normalized == "0,end of file":
        return "audio_eof"
    if re.match(r"^[1-9][0-9]{0,5},", normalized):
        return "nonzero_audio_error"
    return "unclassified"


class AdapterEventType(StrEnum):
    REGISTERING = "registering"
    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    REGISTRATION_ERROR = "registration_error"
    CALL_DIALING = "call_dialing"
    CALL_INCOMING = "call_incoming"
    CALL_RINGING = "call_ringing"
    CALL_ESTABLISHED = "call_established"
    CALL_CLOSED = "call_closed"
    AUDIO_EOF = "audio_eof"


@dataclass(frozen=True, slots=True)
class AdapterEvent:
    type: AdapterEventType
    reason: str | None = None


EventHandler = Callable[[AdapterEvent], Awaitable[None]]


class SipAdapter:
    """Small interface used by the call manager and fake adapter tests."""

    def set_event_handler(self, handler: EventHandler) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def dial(self, number: str) -> None:
        raise NotImplementedError

    async def hangup(self) -> None:
        raise NotImplementedError

    async def start_audio(self, path: Path) -> None:
        raise NotImplementedError

    async def cleanup_call_audio(self) -> None:
        raise NotImplementedError

    async def refresh(self) -> None:
        raise NotImplementedError


class BaresipCtrlTcpAdapter(SipAdapter):
    """Own a baresip process and control it through its loopback TCP module."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._handler: EventHandler | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._ctrl_reader: asyncio.StreamReader | None = None
        self._ctrl_writer: asyncio.StreamWriter | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._ctrl_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[str | tuple[str, str, str]] = asyncio.Queue(
            maxsize=256
        )
        self._command_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False
        self._last_failure_reason: str | None = None

    def set_event_handler(self, handler: EventHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._process is not None and self._process.returncode is None:
                return
            self._stopping = False
            self._write_configuration()
            await self._emit(AdapterEventType.REGISTERING)
            self._process = await asyncio.create_subprocess_exec(
                "baresip",
                "-f",
                str(self._config.baresip_config_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._stdout_task = asyncio.create_task(
                self._read_process_output(), name="baresip-output"
            )
            self._event_task = asyncio.create_task(
                self._event_worker(), name="baresip-events"
            )
            try:
                await self._connect_ctrl()
            except Exception:
                await self._terminate_process()
                await self._cancel_background_tasks()
                self._cleanup_incoming_audio()
                raise
            self._ctrl_task = asyncio.create_task(
                self._read_ctrl_output(), name="baresip-ctrl"
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            if self._ctrl_writer is not None:
                self._ctrl_writer.close()
                try:
                    await self._ctrl_writer.wait_closed()
                except OSError:
                    pass
            self._ctrl_reader = None
            self._ctrl_writer = None
            self._fail_pending("baresip ctrl_tcp stopped")
            await self._terminate_process()
            await self._cancel_background_tasks()
            self._discard_queued_events()
            self._cleanup_incoming_audio()

    async def dial(self, number: str) -> None:
        self._last_failure_reason = None
        # The menu's ausrc command changes Baresip's global default. Restore the
        # persistent source before creating a call: the previous alarm WAV is
        # temporary and may already have been removed by CallManager._finish.
        silence = self._config.baresip_config_dir / "silence.wav"
        await self._command("ausrc", self._aufile_source_parameter(silence))
        host = self._format_host(self._config.sip_server)
        target = f"sip:{number}@{host}:{self._config.sip_port}"
        await self._command("dial", target)

    async def hangup(self) -> None:
        await self._command("hangup")

    async def start_audio(self, path: Path) -> None:
        """Switch from silence to alarm WAV only after CALL_ESTABLISHED."""
        await self._command("ausrc", self._aufile_source_parameter(path))

    @staticmethod
    def _aufile_source_parameter(path: Path) -> str:
        """Keep Baresip's fixed-size audio-device field intact and private."""
        resolved = str(path.resolve())
        if any(character in resolved for character in "\r\n\x00") or len(
            resolved.encode("utf-8")
        ) >= 128:
            raise RuntimeError("Unsafe or too long audio source path")
        return f"aufile,{resolved}"

    async def cleanup_call_audio(self) -> None:
        """Remove received audio that baresip temporarily wrote for the call."""
        self._cleanup_incoming_audio()

    async def refresh(self) -> None:
        writer = self._ctrl_writer
        if (
            self._process is None
            or self._process.returncode is not None
            or writer is None
            or writer.is_closing()
        ):
            await self.stop()
            await self.start()
            return
        response = await self._command("reginfo")
        data = response.get("data")
        if isinstance(data, str):
            await self._parse_line(data[:4096])

    async def _connect_ctrl(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        last_error: OSError | None = None
        while loop.time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError("baresip exited before ctrl_tcp became ready")
            try:
                self._ctrl_reader, self._ctrl_writer = await asyncio.open_connection(
                    self._config.ctrl_host, self._config.ctrl_port
                )
                return
            except OSError as err:
                last_error = err
                await asyncio.sleep(0.25)
        raise RuntimeError("baresip ctrl_tcp did not become ready") from last_error

    async def _command(self, command: str, params: str = "") -> dict[str, Any]:
        if any(character in command + params for character in "\r\n\x00"):
            raise RuntimeError("Unsafe ctrl_tcp command")
        async with self._command_lock:
            writer = self._ctrl_writer
            if writer is None or writer.is_closing():
                raise RuntimeError("baresip ctrl_tcp is unavailable")
            token = str(uuid4())
            payload = json.dumps(
                {"command": command, "params": params, "token": token},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > 8192:
                raise RuntimeError("ctrl_tcp command is too large")
            future = asyncio.get_running_loop().create_future()
            self._pending[token] = future
            try:
                writer.write(str(len(payload)).encode("ascii") + b":" + payload + b",")
                await writer.drain()
                response = await asyncio.wait_for(future, timeout=5)
            except asyncio.TimeoutError as err:
                if command in {"dial", "ausrc", "hangup"}:
                    _LOGGER.warning("Baresip command %s timed out", command)
                raise RuntimeError("baresip ctrl_tcp response timed out") from err
            finally:
                self._pending.pop(token, None)
            if response.get("ok") is not True:
                if command in {"dial", "ausrc", "hangup"}:
                    _LOGGER.warning("Baresip command %s rejected", command)
                raise RuntimeError("baresip rejected the ctrl_tcp command")
            if command == "ausrc":
                data = response.get("data")
                normalized = data.strip().lower() if isinstance(data, str) else ""
                if any(
                    marker in normalized
                    for marker in (
                        "failed to set audio-source",
                        "no such audio-source",
                        "no such device for",
                        "no config object",
                        "format should be:",
                    )
                ) or not normalized.startswith("switch audio device: "):
                    _LOGGER.warning("Baresip command ausrc source_switch_failed")
                    raise RuntimeError("baresip could not switch the audio source")
            if command in {"dial", "ausrc", "hangup"}:
                _LOGGER.info("Baresip command %s accepted", command)
            return response

    async def _read_process_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                decoded = line.decode("utf-8", errors="replace")[:4096]
                self._log_process_diagnostic(decoded)
                await self._parse_line(decoded)
            return_code = await process.wait()
            if not self._stopping:
                _LOGGER.error("baresip stopped unexpectedly with code %s", return_code)
                await self._emit(
                    AdapterEventType.REGISTRATION_ERROR, "SIP process stopped"
                )
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _log_process_diagnostic(line: str) -> None:
        """Log only fixed, known Baresip warning categories, never raw output."""
        normalized = line.strip().lower()
        if normalized.startswith("warning: "):
            normalized = normalized[len("warning: ") :]
        for prefix, cause in _PROCESS_CAUSES:
            if normalized.startswith(prefix):
                _LOGGER.warning("Baresip diagnostic cause=%s", cause)
                return

    async def _read_ctrl_output(self) -> None:
        reader = self._ctrl_reader
        if reader is None:
            return
        try:
            while True:
                prefix = await reader.readuntil(b":")
                length_text = prefix[:-1]
                if not length_text.isdigit() or len(length_text) > 7:
                    raise RuntimeError("Invalid baresip ctrl_tcp netstring length")
                length = int(length_text)
                if length < 2 or length > 1024 * 1024:
                    raise RuntimeError("Baresip ctrl_tcp frame exceeds the limit")
                payload = await reader.readexactly(length)
                if await reader.readexactly(1) != b",":
                    raise RuntimeError("Invalid baresip ctrl_tcp netstring terminator")
                try:
                    message = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise RuntimeError("Invalid baresip ctrl_tcp JSON") from err
                if not isinstance(message, dict):
                    raise RuntimeError("Invalid baresip ctrl_tcp message")
                await self._handle_ctrl_message(message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, RuntimeError):
            if not self._stopping:
                writer = self._ctrl_writer
                self._ctrl_reader = None
                self._ctrl_writer = None
                if writer is not None:
                    writer.close()
                self._fail_pending("baresip ctrl_tcp connection lost")
                await self._emit(
                    AdapterEventType.REGISTRATION_ERROR, "SIP control connection lost"
                )
        except asyncio.CancelledError:
            raise

    async def _handle_ctrl_message(self, message: dict[str, Any]) -> None:
        token = message.get("token")
        if message.get("response") is True and isinstance(token, str):
            future = self._pending.get(token)
            if future is not None and not future.done():
                future.set_result(message)
            return
        event = message.get("event")
        if event is True and isinstance(message.get("type"), str):
            event_name = message["type"]
            event_class = message.get("class")
            parameter = message.get("param")
            detail = parameter[:4096] if isinstance(parameter, str) else ""
            if event_class == "call" and event_name in _DIAGNOSTIC_CALL_EVENTS:
                if event_name == "CALL_CLOSED":
                    match = _SIP_STATUS.match(detail)
                    code = match.group(1) if match else "unknown"
                    _LOGGER.info(
                        "Baresip call event CALL_CLOSED sip_code=%s cause=%s",
                        code,
                        _close_cause(detail),
                    )
                else:
                    _LOGGER.info("Baresip call event %s", event_name)
            elif event_class == "other" and event_name == "AUDIO_ERROR":
                _LOGGER.info(
                    "Baresip media event AUDIO_ERROR cause=%s",
                    _audio_error_cause(detail),
                )
            elif event_class == "other" and event_name == "CALL_REMOTE_SDP":
                kind = detail if detail in {"offer", "answer"} else "unknown"
                _LOGGER.info("Baresip media event CALL_REMOTE_SDP kind=%s", kind)
            elif event_class == "other" and event_name == "CALL_RTPESTAB":
                _LOGGER.info("Baresip media event %s", event_name)
            self._queue_event(
                (event_class if isinstance(event_class, str) else "", event_name, detail)
            )
        elif isinstance(event, str):
            data = message.get("data")
            detail = data if isinstance(data, str) else ""
            self._queue_event(f"{event} {detail}"[:4096])

    def _queue_event(self, event: str | tuple[str, str, str]) -> None:
        """Queue events so ctrl_tcp responses can never deadlock behind handlers."""
        if self._event_task is None or self._event_task.done():
            self._event_task = asyncio.create_task(
                self._event_worker(), name="baresip-events"
            )
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            _LOGGER.error("Baresip event queue is full; marking registration unhealthy")
            asyncio.create_task(
                self._emit(AdapterEventType.REGISTRATION_ERROR, "SIP event queue overflow")
            )

    async def _event_worker(self) -> None:
        try:
            while True:
                event = await self._event_queue.get()
                try:
                    if isinstance(event, tuple):
                        await self._parse_ctrl_event(*event)
                    else:
                        await self._parse_line(event)
                finally:
                    self._event_queue.task_done()
        except asyncio.CancelledError:
            raise

    async def wait_for_queued_events(self) -> None:
        """Wait for already queued events; used by deterministic adapter tests."""
        await self._event_queue.join()

    async def _parse_ctrl_event(
        self, event_class: str, event_name: str, detail: str
    ) -> None:
        """Use the exact ctrl_tcp event type, never a substring in its param."""
        if event_class in {"register", "ua"}:
            if event_name in {"REGISTER_OK", "REGISTERED"}:
                await self._emit(AdapterEventType.REGISTERED)
            elif event_name in {"REGISTER_FAIL", "REGISTER_ERROR"}:
                await self._emit(
                    AdapterEventType.REGISTRATION_ERROR, "SIP registration failed"
                )
            elif event_name in {"UNREGISTERING", "UNREGISTERED"}:
                await self._emit(AdapterEventType.UNREGISTERED)
            return

        if event_class == "audio":
            if event_name in {"AUDIO_EOF", "END_OF_FILE"}:
                await self._emit(AdapterEventType.AUDIO_EOF)
            return

        if event_class != "call":
            return
        if event_name == "CALL_INCOMING":
            await self._emit(AdapterEventType.CALL_INCOMING)
        elif event_name == "CALL_OUTGOING":
            await self._emit(AdapterEventType.CALL_DIALING)
        elif event_name == "CALL_RINGING":
            await self._emit(AdapterEventType.CALL_RINGING)
        elif event_name == "CALL_ESTABLISHED":
            await self._emit(AdapterEventType.CALL_ESTABLISHED)
        elif event_name == "CALL_CLOSED":
            match = _SIP_STATUS.match(detail)
            code = match.group(1) if match else None
            if code == "486":
                reason = "busy"
            elif code == "408" or "NO ANSWER" in detail.upper():
                reason = "no_answer"
            elif code is not None and int(code) >= 300:
                reason = "failed"
            else:
                reason = self._last_failure_reason or "normal"
            self._last_failure_reason = None
            await self._emit(AdapterEventType.CALL_CLOSED, reason)

    async def _parse_line(self, line: str) -> None:
        upper = line.upper()
        if "REGISTER_OK" in upper or re.search(r"\bREGISTERED\b", upper):
            await self._emit(AdapterEventType.REGISTERED)
        elif any(token in upper for token in ("REGISTER_FAIL", "REGISTER_ERROR")):
            await self._emit(
                AdapterEventType.REGISTRATION_ERROR, "SIP registration failed"
            )
        elif "UNREGISTERING" in upper or "UNREGISTERED" in upper:
            await self._emit(AdapterEventType.UNREGISTERED)

        if "CALL_INCOMING" in upper:
            await self._emit(AdapterEventType.CALL_INCOMING)
        if "CALL_OUTGOING" in upper:
            await self._emit(AdapterEventType.CALL_DIALING)
        if "CALL_RINGING" in upper or "180 RINGING" in upper:
            await self._emit(AdapterEventType.CALL_RINGING)
        if "CALL_ESTABLISHED" in upper:
            await self._emit(AdapterEventType.CALL_ESTABLISHED)

        if "486 BUSY" in upper or "CALL_BUSY" in upper:
            self._last_failure_reason = "busy"
        elif any(token in upper for token in ("408 REQUEST TIMEOUT", "NO ANSWER")):
            self._last_failure_reason = "no_answer"
        elif any(token in upper for token in ("CALL_FAILED", "DECLINED", "FORBIDDEN")):
            self._last_failure_reason = "failed"

        if "CALL_CLOSED" in upper:
            reason = self._last_failure_reason or "normal"
            self._last_failure_reason = None
            await self._emit(AdapterEventType.CALL_CLOSED, reason)

        lower = line.lower().strip()
        if (
            lower == "aufile: end of file"
            or "audio_eof" in lower
            or "end_of_file" in lower
        ):
            await self._emit(AdapterEventType.AUDIO_EOF)

    async def _emit(self, event_type: AdapterEventType, reason: str | None = None) -> None:
        if self._handler is None:
            return
        try:
            await self._handler(AdapterEvent(event_type, reason))
        except Exception:
            _LOGGER.exception("Unhandled SIP adapter event")

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _cancel_background_tasks(self) -> None:
        tasks = tuple(
            task
            for task in (self._ctrl_task, self._stdout_task, self._event_task)
            if task is not None and task is not asyncio.current_task()
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ctrl_task = None
        self._stdout_task = None
        self._event_task = None

    def _discard_queued_events(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._event_queue.task_done()

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    def _write_configuration(self) -> None:
        directory = self._config.baresip_config_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        silence = directory / "silence.wav"
        self._write_silence(silence)

        runtime = self._config.runtime_dir
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.chmod(0o700)
        self._cleanup_incoming_audio()
        incoming = runtime / "incoming.wav"
        lines = [
            "module_path /usr/lib/baresip/modules",
            "poll_method poll",
            "sip_listen 0.0.0.0:0",
        ]
        if self._config.sip_transport == "tls":
            lines.extend(
                (
                    "sip_cafile /etc/ssl/certs/ca-certificates.crt",
                    "sip_verify_server yes",
                )
            )
        lines.extend(
            (
                "call_local_timeout 5",
                "call_max_calls 1",
                "rtp_ports 10000-10019",
                "audio_path .",
                f"audio_player aufile,{incoming}",
                f"audio_source aufile,{silence}",
                "ausrc_srate 8000",
                "auplay_srate 8000",
                "ausrc_channels 1",
                "auplay_channels 1",
                "module g711.so",
                "module aufile.so",
                "module_tmp account.so",
                "module_app menu.so",
                "module_app ctrl_tcp.so",
                f"ctrl_tcp_listen {self._config.ctrl_host}:{self._config.ctrl_port}",
                "",
            )
        )
        config_text = "\n".join(lines)
        self._secure_write(directory / "config", config_text)

        host = self._format_host(self._config.sip_server)
        account = (
            f"<sip:{self._config.sip_extension}@{host}:{self._config.sip_port}"
            f";transport={self._config.sip_transport}>"
            f";auth_user={self._config.sip_extension}"
            f";auth_pass={self._config.sip_password};regint=300"
        )
        if self._config.outbound_proxy:
            account += f';outbound="{self._config.outbound_proxy}"'
        self._secure_write(directory / "accounts", account + "\n")
        self._secure_write(directory / "contacts", "")

    def _cleanup_incoming_audio(self) -> None:
        try:
            (self._config.runtime_dir / "incoming.wav").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.warning("Unable to remove temporary received-call audio")

    @staticmethod
    def _write_silence(path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 8000 * 125)
        path.chmod(0o600)

    @staticmethod
    def _secure_write(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _format_host(host: str) -> str:
        return f"[{host}]" if ":" in host and not host.startswith("[") else host
