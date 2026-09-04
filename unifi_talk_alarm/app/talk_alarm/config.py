"""Validated add-on configuration and outbound number policy."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
from typing import Any

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_EXTENSION = re.compile(r"^[0-9A-Za-z+*#()._-]{1,64}$")
_VOICE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
_DIAL = re.compile(r"^[0-9+*#]{1,32}$")
_HARD_BLOCKED_EMERGENCY_NUMBERS = frozenset(
    {
        "110",
        "112",
        "911",
        "999",
        "49110",
        "49112",
        "0049110",
        "0049112",
    }
)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _string(options: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = options.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if (not value and not allow_empty) or _has_control(value):
        raise ValueError(f"{key} is empty or contains control characters")
    return value


def _integer(
    options: dict[str, Any], key: str, minimum: int, maximum: int
) -> int:
    value = options.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{key} must be an integer") from err
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _floating(
    options: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    value = options.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        value = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{key} must be a number") from err
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _validate_host(value: str, *, key: str) -> str:
    value = value.strip().rstrip(".").lower()
    if not value or len(value) > 253 or any(character.isspace() for character in value):
        raise ValueError(f"{key} is not a valid host")
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    if any(not _HOST_LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError(f"{key} is not a valid host")
    return value


def _string_list(options: dict[str, Any], key: str) -> tuple[str, ...]:
    values = options.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{key} must contain at least one item")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip() or _has_control(item):
            raise ValueError(f"{key} contains an invalid item")
        result.append(item.strip())
    return tuple(result)


class NumberPolicy:
    """Small, non-regex allowlist that cannot suffer regex backtracking."""

    def __init__(
        self, rules: tuple[str, ...], blocked_rules: tuple[str, ...] = ()
    ) -> None:
        self._rules = self._normalize(rules)
        self._blocked_rules = self._normalize(blocked_rules)

    @staticmethod
    def _normalize(rules: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        normalized: list[tuple[str, str]] = []
        for rule in rules:
            if rule == "*":
                normalized.append(("all", ""))
                continue
            if rule.startswith("prefix:"):
                value = rule.removeprefix("prefix:").strip()
                kind = "prefix"
            elif rule.startswith("exact:"):
                value = rule.removeprefix("exact:").strip()
                kind = "exact"
            else:
                value = rule
                kind = "exact"
            if not value or len(value) > 64 or not _DIAL.fullmatch(value):
                raise ValueError("allowed_number_rules contains an invalid dial string")
            normalized.append((kind, value))
        return tuple(normalized)

    @staticmethod
    def _matches(number: str, rules: tuple[tuple[str, str], ...]) -> bool:
        return any(
            kind == "all"
            or (kind == "exact" and number == value)
            or (kind == "prefix" and number.startswith(value))
            for kind, value in rules
        )

    @staticmethod
    def _dialed_digits(number: str) -> str:
        """Extract the dial target for non-bypassable emergency blocking."""
        return re.sub(r"[^0-9]", "", number)

    def allows(self, number: str) -> bool:
        """Return whether the dial string is explicitly allowed."""
        if self._dialed_digits(number) in _HARD_BLOCKED_EMERGENCY_NUMBERS:
            return False
        if self._matches(number, self._blocked_rules):
            return False
        return self._matches(number, self._rules)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully validated runtime configuration."""

    sip_server: str
    sip_port: int
    sip_extension: str
    sip_password: str
    sip_transport: str
    outbound_proxy: str | None
    api_token: str
    api_port: int
    allowed_number_rules: tuple[str, ...]
    blocked_number_rules: tuple[str, ...]
    allowed_audio_hosts: frozenset[str]
    tts_voice: str
    tts_speed: int
    max_calls_per_window: int
    rate_limit_window_seconds: int
    max_audio_bytes: int
    max_audio_seconds: int
    hangup_buffer_seconds: float
    terminal_state_seconds: int
    log_level: str
    baresip_config_dir: Path = Path("/data/baresip")
    runtime_dir: Path = Path("/tmp/unifi-talk-alarm")
    ctrl_host: str = "127.0.0.1"
    ctrl_port: int = 4444

    @classmethod
    def from_mapping(cls, options: dict[str, Any]) -> AppConfig:
        """Construct and validate configuration from Supervisor options."""
        server = _validate_host(_string(options, "sip_server"), key="sip_server")
        extension = _string(options, "sip_extension")
        if not _EXTENSION.fullmatch(extension):
            raise ValueError("sip_extension contains unsupported characters")

        password = options.get("sip_password")
        if (
            not isinstance(password, str)
            or not password
            or len(password) > 256
            or _has_control(password)
            or any(character in password for character in ';"\\<>')
        ):
            raise ValueError(
                "sip_password is empty, too long, or contains bare SIP syntax delimiters"
            )

        token = options.get("api_token")
        if (
            not isinstance(token, str)
            or len(token) < 32
            or token != token.strip()
            or len(token) > 512
            or any(character.isspace() for character in token)
            or _has_control(token)
        ):
            raise ValueError(
                "api_token must contain 32 to 512 non-whitespace characters"
            )

        transport = _string(options, "sip_transport").lower()
        if transport not in {"udp", "tcp", "tls"}:
            raise ValueError("sip_transport must be udp, tcp, or tls")

        outbound = _string(options, "outbound_proxy", allow_empty=True)
        if outbound:
            if (
                len(outbound) > 512
                or not outbound.lower().startswith(("sip:", "sips:"))
                or any(character.isspace() for character in outbound)
                or any(character in outbound for character in '"\\<>')
            ):
                raise ValueError("outbound_proxy must be a SIP URI")

        voice = _string(options, "tts_voice")
        if not _VOICE.fullmatch(voice):
            raise ValueError("tts_voice contains unsupported characters")

        rules = _string_list(options, "allowed_number_rules")
        blocked_rules = _string_list(options, "blocked_number_rules")
        NumberPolicy(rules, blocked_rules)
        audio_hosts = frozenset(
            _validate_host(host, key="allowed_audio_hosts")
            for host in _string_list(options, "allowed_audio_hosts")
        )

        log_level = _string(options, "log_level").lower()
        if log_level not in {"debug", "info", "warning", "error"}:
            raise ValueError("log_level is invalid")

        return cls(
            sip_server=server,
            sip_port=_integer(options, "sip_port", 1, 65535),
            sip_extension=extension,
            sip_password=password,
            sip_transport=transport,
            outbound_proxy=outbound or None,
            api_token=token,
            api_port=_integer(options, "api_port", 1, 65535),
            allowed_number_rules=rules,
            blocked_number_rules=blocked_rules,
            allowed_audio_hosts=audio_hosts,
            tts_voice=voice,
            tts_speed=_integer(options, "tts_speed", 80, 450),
            max_calls_per_window=_integer(options, "max_calls_per_window", 1, 100),
            rate_limit_window_seconds=_integer(
                options, "rate_limit_window_seconds", 10, 86400
            ),
            max_audio_bytes=_integer(options, "max_audio_bytes", 65536, 52428800),
            max_audio_seconds=_integer(options, "max_audio_seconds", 1, 600),
            hangup_buffer_seconds=_floating(
                options, "hangup_buffer_seconds", 0.0, 10.0
            ),
            terminal_state_seconds=_integer(
                options, "terminal_state_seconds", 2, 300
            ),
            log_level=log_level,
        )

    @classmethod
    def from_file(cls, path: Path = Path("/data/options.json")) -> AppConfig:
        """Load Supervisor options without ever logging their values."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise ValueError("Unable to read add-on options") from err
        if not isinstance(payload, dict):
            raise ValueError("Add-on options must be a JSON object")
        return cls.from_mapping(payload)

    @property
    def number_policy(self) -> NumberPolicy:
        return NumberPolicy(self.allowed_number_rules, self.blocked_number_rules)
