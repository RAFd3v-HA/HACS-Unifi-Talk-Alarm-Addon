"""Test fixtures for the add-on API and state machine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).parents[1] / "unifi_talk_alarm" / "app"
sys.path.insert(0, str(APP_ROOT))

from talk_alarm.config import AppConfig  # noqa: E402


@pytest.fixture
def options() -> dict:
    return {
        "sip_server": "192.168.1.1",
        "sip_port": 5060,
        "sip_extension": "150",
        "sip_password": "safe-password_123",
        "sip_transport": "udp",
        "outbound_proxy": "",
        "api_token": "test-token-with-at-least-32-characters",
        "api_port": 8099,
        "allowed_number_rules": ["prefix:1", "prefix:+49"],
        "blocked_number_rules": ["exact:110", "exact:112", "exact:911", "exact:999"],
        "allowed_audio_hosts": ["homeassistant.local"],
        "tts_voice": "de",
        "tts_speed": 160,
        "max_calls_per_window": 3,
        "rate_limit_window_seconds": 300,
        "max_audio_bytes": 5_242_880,
        "max_audio_seconds": 120,
        "hangup_buffer_seconds": 1.0,
        "terminal_state_seconds": 15,
        "log_level": "info",
    }


@pytest.fixture
def app_config(options: dict, tmp_path: Path) -> AppConfig:
    config = AppConfig.from_mapping(options)
    object.__setattr__(config, "baresip_config_dir", tmp_path / "baresip")
    object.__setattr__(config, "runtime_dir", tmp_path / "runtime")
    return config
