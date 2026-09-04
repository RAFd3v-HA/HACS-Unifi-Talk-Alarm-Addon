"""Public state models for API v1."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RegistrationState(StrEnum):
    REGISTERED = "registered"
    REGISTERING = "registering"
    UNREGISTERED = "unregistered"
    ERROR = "error"


class CallState(StrEnum):
    IDLE = "idle"
    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    ENDING = "ending"
    ENDED = "ended"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    FAILED = "failed"


ACTIVE_CALL_STATES = frozenset(
    {CallState.DIALING, CallState.RINGING, CallState.CONNECTED, CallState.ENDING}
)
TERMINAL_CALL_STATES = frozenset(
    {CallState.ENDED, CallState.BUSY, CallState.NO_ANSWER, CallState.FAILED}
)


@dataclass(slots=True)
class RegistrationSnapshot:
    state: str = RegistrationState.REGISTERING
    detail: str | None = "Starting SIP client"
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallSnapshot:
    state: str = CallState.IDLE
    call_id: str | None = None
    number: str | None = None
    direction: str | None = None
    started_at: str | None = None
    answered_at: str | None = None
    ended_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

