"""Single-call state machine and safety controls."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable
from dataclasses import replace
import logging
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .adapters import AdapterEvent, AdapterEventType, SipAdapter
from .audio import AudioProcessor, PreparedAudio
from .config import AppConfig
from .errors import ServiceError
from .models import (
    ACTIVE_CALL_STATES,
    TERMINAL_CALL_STATES,
    CallSnapshot,
    CallState,
    RegistrationSnapshot,
    RegistrationState,
    utc_now,
)

_LOGGER = logging.getLogger(__name__)


class AudioProvider(Protocol):
    async def from_message(self, message: str) -> PreparedAudio: ...

    async def from_url(self, audio_url: str) -> PreparedAudio: ...

    async def from_wav_bytes(self, audio_wav: bytes) -> PreparedAudio: ...


class CallManager:
    """Serialize calls and map adapter events to API v1 states."""

    def __init__(
        self,
        config: AppConfig,
        adapter: SipAdapter,
        audio: AudioProvider | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._audio = audio or AudioProcessor(config)
        self._lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._registration = RegistrationSnapshot(updated_at=utc_now())
        self._call = CallSnapshot()
        self._prepared: PreparedAudio | None = None
        self._sip_call_started = False
        self._attempts: deque[float] = deque()
        self._ring_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._terminal_task: asyncio.Task[None] | None = None
        self._ending_task: asyncio.Task[None] | None = None
        adapter.set_event_handler(self.handle_adapter_event)

    async def status(self) -> tuple[RegistrationSnapshot, CallSnapshot]:
        """Return a detached state snapshot."""
        async with self._lock:
            return replace(self._registration), replace(self._call)

    async def start_call(
        self,
        *,
        number: str,
        message: str | None,
        audio_url: str | None,
        audio_wav: bytes | None = None,
        ring_timeout: int,
    ) -> CallSnapshot:
        """Prepare media, dial once, and return the initial call state."""
        if sum(source is not None for source in (message, audio_url, audio_wav)) != 1:
            raise ServiceError(
                422,
                "invalid_audio_source",
                "Exactly one of message, audio_url and audio_wav_base64 is required",
            )
        if not self._config.number_policy.allows(number):
            raise ServiceError(
                422,
                "number_not_allowed",
                "The destination is blocked by the outbound number policy",
            )

        call_id = str(uuid4())
        async with self._lock:
            if self._call.state in ACTIVE_CALL_STATES:
                raise ServiceError(409, "call_in_progress", "Another call is active")
            self._check_rate_limit(record=False)
            self._cancel_task(self._terminal_task)
            self._terminal_task = None
            self._call = CallSnapshot(
                state=CallState.DIALING,
                call_id=call_id,
                number=number,
                direction="outbound",
                started_at=utc_now(),
            )
            self._sip_call_started = False

        try:
            if message is not None:
                prepared = await self._audio.from_message(message)
            elif audio_url is not None:
                prepared = await self._audio.from_url(audio_url)
            else:
                prepared = await self._audio.from_wav_bytes(audio_wav or b"")
        except ServiceError:
            await self._finish(call_id, CallState.FAILED, "audio_preparation_failed")
            raise
        except Exception as err:
            await self._finish(call_id, CallState.FAILED, "audio_preparation_failed")
            raise ServiceError(500, "audio_preparation_failed", "Unable to prepare audio") from err

        async with self._operation_lock:
            rate_error: ServiceError | None = None
            async with self._lock:
                if self._call.call_id != call_id or self._call.state not in {
                    CallState.DIALING,
                    CallState.RINGING,
                }:
                    cancelled = True
                else:
                    cancelled = False
                    try:
                        self._check_rate_limit(record=True)
                    except ServiceError as err:
                        rate_error = err
                    else:
                        self._prepared = prepared
                        self._sip_call_started = True
            if cancelled:
                prepared.cleanup()
                raise ServiceError(409, "call_cancelled", "The call was cancelled")
            if rate_error is not None:
                prepared.cleanup()
                await self._finish(call_id, CallState.FAILED, "rate_limited")
                raise rate_error

            try:
                await self._adapter.dial(number)
            except Exception as err:
                try:
                    await self._adapter.hangup()
                except Exception:
                    _LOGGER.warning("SIP cleanup failed after an uncertain dial result")
                await self._finish(call_id, CallState.FAILED, "sip_dial_failed")
                raise ServiceError(
                    503, "sip_dial_failed", "The SIP client could not dial"
                ) from err

            async with self._lock:
                if self._call.call_id == call_id and self._call.state in {
                    CallState.DIALING,
                    CallState.RINGING,
                }:
                    self._ring_task = asyncio.create_task(
                        self._ring_timeout(call_id, ring_timeout),
                        name=f"ring-timeout-{call_id}",
                    )
                return replace(self._call)

    async def hangup(self, call_id: str) -> CallSnapshot:
        """Begin ending the matching active call."""
        async with self._operation_lock:
            async with self._lock:
                if self._call.call_id != call_id:
                    raise ServiceError(404, "call_not_found", "Unknown or expired call ID")
                if self._call.state not in ACTIVE_CALL_STATES - {CallState.ENDING}:
                    raise ServiceError(
                        409, "call_not_active", "The call can no longer be ended"
                    )
                self._call.state = CallState.ENDING
                self._cancel_media_timers()
                sip_call_started = self._sip_call_started
            if not sip_call_started:
                await self._finish(call_id, CallState.ENDED, None)
                async with self._lock:
                    return replace(self._call)
            try:
                await self._adapter.hangup()
            except Exception as err:
                await self._finish(call_id, CallState.FAILED, "sip_hangup_failed")
                raise ServiceError(
                    503, "sip_hangup_failed", "The SIP client could not hang up"
                ) from err
            async with self._lock:
                if self._call.call_id == call_id and self._call.state == CallState.ENDING:
                    self._ending_task = asyncio.create_task(
                        self._ending_fallback(call_id), name=f"ending-fallback-{call_id}"
                    )
                return replace(self._call)

    async def refresh(self) -> None:
        """Ask the SIP adapter to refresh its registration view."""
        try:
            await self._adapter.refresh()
        except Exception as err:
            await self.set_registration_error("SIP refresh failed")
            raise ServiceError(503, "sip_refresh_failed", "Unable to refresh SIP state") from err

    async def set_registration_error(self, detail: str) -> None:
        async with self._lock:
            self._registration = RegistrationSnapshot(
                state=RegistrationState.ERROR,
                detail=detail[:128],
                updated_at=utc_now(),
            )

    async def handle_adapter_event(self, event: AdapterEvent) -> None:
        """Handle normalized adapter events; never consume raw SIP log lines."""
        if event.type in {
            AdapterEventType.REGISTERING,
            AdapterEventType.REGISTERED,
            AdapterEventType.UNREGISTERED,
            AdapterEventType.REGISTRATION_ERROR,
        }:
            state = {
                AdapterEventType.REGISTERING: RegistrationState.REGISTERING,
                AdapterEventType.REGISTERED: RegistrationState.REGISTERED,
                AdapterEventType.UNREGISTERED: RegistrationState.UNREGISTERED,
                AdapterEventType.REGISTRATION_ERROR: RegistrationState.ERROR,
            }[event.type]
            detail = {
                RegistrationState.REGISTERING: "Registering with Talk",
                RegistrationState.REGISTERED: "Registered with Talk",
                RegistrationState.UNREGISTERED: "Not registered with Talk",
                RegistrationState.ERROR: event.reason or "SIP registration failed",
            }[state]
            async with self._lock:
                self._registration = RegistrationSnapshot(
                    state=state, detail=detail[:128], updated_at=utc_now()
                )
            return

        if event.type == AdapterEventType.CALL_INCOMING:
            await self._reject_incoming_call()
            return

        async with self._lock:
            call_id = self._call.call_id
            if (
                call_id is None
                or self._call.state == CallState.IDLE
                or not self._sip_call_started
            ):
                return

        if event.type == AdapterEventType.CALL_DIALING:
            await self._set_call_state(call_id, CallState.DIALING)
        elif event.type == AdapterEventType.CALL_RINGING:
            await self._set_call_state(call_id, CallState.RINGING)
        elif event.type == AdapterEventType.CALL_ESTABLISHED:
            await self._on_established(call_id)
        elif event.type == AdapterEventType.AUDIO_EOF:
            await self._hangup_after_audio(call_id)
        elif event.type == AdapterEventType.CALL_CLOSED:
            async with self._lock:
                previous = self._call.state if self._call.call_id == call_id else None
            if previous in TERMINAL_CALL_STATES:
                return
            if previous == CallState.ENDING or previous == CallState.CONNECTED:
                state = CallState.ENDED
                reason = None
            elif event.reason == "busy":
                state, reason = CallState.BUSY, "busy"
            elif event.reason == "no_answer":
                state, reason = CallState.NO_ANSWER, "no_answer"
            else:
                state, reason = CallState.FAILED, "sip_call_failed"
            await self._finish(call_id, state, reason)

    async def shutdown(self) -> None:
        """Stop timers, media and the SIP adapter."""
        async with self._operation_lock:
            async with self._lock:
                for task in (
                    self._ring_task,
                    self._audio_task,
                    self._terminal_task,
                    self._ending_task,
                ):
                    self._cancel_task(task)
                self._cleanup_audio()
                self._sip_call_started = False
            await self._adapter.stop()
            await self._cleanup_adapter_audio()

    async def _on_established(self, call_id: str) -> None:
        async with self._operation_lock:
            async with self._lock:
                if self._call.call_id != call_id or self._call.state not in {
                    CallState.DIALING,
                    CallState.RINGING,
                }:
                    return
                prepared = self._prepared
                if prepared is None:
                    missing = True
                else:
                    missing = False
                    self._call.state = CallState.CONNECTED
                    self._call.answered_at = utc_now()
                    self._cancel_task(self._ring_task)
                    self._ring_task = None
            if missing:
                try:
                    await self._adapter.hangup()
                except Exception:
                    pass
                await self._finish(call_id, CallState.FAILED, "audio_missing")
                return

            # This is intentionally the sole point at which alarm WAV becomes a source.
            try:
                await self._adapter.start_audio(prepared.path)
            except Exception:
                try:
                    await self._adapter.hangup()
                except Exception:
                    pass
                await self._finish(call_id, CallState.FAILED, "audio_start_failed")
                return
            async with self._lock:
                if self._call.call_id == call_id and self._call.state == CallState.CONNECTED:
                    self._audio_task = asyncio.create_task(
                        self._audio_deadline(
                            call_id, prepared.duration + self._config.hangup_buffer_seconds
                        ),
                        name=f"audio-deadline-{call_id}",
                    )

    async def _hangup_after_audio(self, call_id: str) -> None:
        async with self._operation_lock:
            async with self._lock:
                if self._call.call_id != call_id or self._call.state != CallState.CONNECTED:
                    return
                self._call.state = CallState.ENDING
                self._cancel_media_timers()
            try:
                await self._adapter.hangup()
            except Exception:
                await self._finish(call_id, CallState.FAILED, "sip_hangup_failed")
                return
            async with self._lock:
                if self._call.call_id == call_id and self._call.state == CallState.ENDING:
                    self._ending_task = asyncio.create_task(
                        self._ending_fallback(call_id), name=f"ending-fallback-{call_id}"
                    )

    async def _ring_timeout(self, call_id: str, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            async with self._operation_lock:
                async with self._lock:
                    if self._call.call_id != call_id or self._call.state not in {
                        CallState.DIALING,
                        CallState.RINGING,
                    }:
                        return
                    self._call.state = CallState.ENDING
                try:
                    await self._adapter.hangup()
                except Exception:
                    await self._finish(call_id, CallState.FAILED, "sip_hangup_failed")
                    return
                await self._finish(call_id, CallState.NO_ANSWER, "no_answer")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._finish(call_id, CallState.FAILED, "sip_hangup_failed")

    async def _audio_deadline(self, call_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            await self._hangup_after_audio(call_id)
        except asyncio.CancelledError:
            raise

    async def _ending_fallback(self, call_id: str) -> None:
        try:
            await asyncio.sleep(5)
            await self._finish(call_id, CallState.ENDED, None)
        except asyncio.CancelledError:
            raise

    async def _set_call_state(self, call_id: str, state: CallState) -> None:
        async with self._lock:
            if self._call.call_id == call_id and self._call.state in {
                CallState.DIALING,
                CallState.RINGING,
            }:
                self._call.state = state

    async def _finish(
        self, call_id: str, state: CallState, error: str | None
    ) -> None:
        async with self._lock:
            if self._call.call_id != call_id:
                return
            self._cancel_media_timers()
            self._cancel_task(self._ending_task)
            self._ending_task = None
            self._call.state = state
            self._call.error = error
            self._call.ended_at = utc_now()
            self._cleanup_audio()
            self._sip_call_started = False
        await self._cleanup_adapter_audio()
        await self._schedule_terminal_reset(call_id)

    async def _reject_incoming_call(self) -> None:
        """Reject inbound calls without disrupting an active outbound call."""
        async with self._operation_lock:
            async with self._lock:
                outbound_active = self._sip_call_started and self._call.state in ACTIVE_CALL_STATES
            if outbound_active:
                _LOGGER.warning("Rejected an inbound call while the outbound slot was occupied")
                return
            try:
                await self._adapter.hangup()
            except Exception:
                _LOGGER.warning("Unable to reject an inbound SIP call")

    async def _cleanup_adapter_audio(self) -> None:
        try:
            await self._adapter.cleanup_call_audio()
        except Exception:
            _LOGGER.warning("Unable to remove temporary received-call audio")

    async def _schedule_terminal_reset(self, call_id: str) -> None:
        async with self._lock:
            self._cancel_task(self._terminal_task)
            self._terminal_task = asyncio.create_task(
                self._terminal_reset(call_id), name=f"terminal-reset-{call_id}"
            )

    async def _terminal_reset(self, call_id: str) -> None:
        try:
            await asyncio.sleep(self._config.terminal_state_seconds)
            async with self._lock:
                if self._call.call_id != call_id or self._call.state not in TERMINAL_CALL_STATES:
                    return
                if self._call.state != CallState.ENDED:
                    self._call.state = CallState.ENDED
            await asyncio.sleep(2)
            async with self._lock:
                if self._call.call_id == call_id and self._call.state == CallState.ENDED:
                    self._call = CallSnapshot()
        except asyncio.CancelledError:
            raise

    def _check_rate_limit(self, *, record: bool) -> None:
        now = asyncio.get_running_loop().time()
        cutoff = now - self._config.rate_limit_window_seconds
        while self._attempts and self._attempts[0] <= cutoff:
            self._attempts.popleft()
        if len(self._attempts) >= self._config.max_calls_per_window:
            raise ServiceError(
                429,
                "rate_limited",
                "The configured outbound call rate limit was reached",
            )
        if record:
            self._attempts.append(now)

    def _cancel_media_timers(self) -> None:
        self._cancel_task(self._ring_task)
        self._ring_task = None
        self._cancel_task(self._audio_task)
        self._audio_task = None

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _cleanup_audio(self) -> None:
        if self._prepared is not None:
            self._prepared.cleanup()
            self._prepared = None
