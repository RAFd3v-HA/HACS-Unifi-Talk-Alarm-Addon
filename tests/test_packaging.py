"""Static packaging checks for the standalone Home Assistant add-on."""

import json
from pathlib import Path
import re

from talk_alarm import __version__
from talk_alarm.__main__ import API_BIND_HOST


ROOT = Path(__file__).parents[1]
ADDON = ROOT / "unifi_talk_alarm"

CORE_FIELD_LABELS = {
    "sip_server": "SIP Server Host",
    "sip_port": "SIP Server Port",
    "sip_extension": "SIP Username",
    "sip_password": "SIP Password",
}
OPTION_KEYS = {
    *CORE_FIELD_LABELS,
    "sip_transport",
    "outbound_proxy",
    "api_token",
    "api_port",
    "allowed_number_rules",
    "blocked_number_rules",
    "allowed_audio_hosts",
    "tts_voice",
    "tts_speed",
    "max_calls_per_window",
    "rate_limit_window_seconds",
    "max_audio_bytes",
    "max_audio_seconds",
    "hangup_buffer_seconds",
    "terminal_state_seconds",
    "log_level",
}


def test_runtime_api_is_loopback_only() -> None:
    assert API_BIND_HOST == "127.0.0.1"


def test_addon_and_runtime_versions_match() -> None:
    """A version bump must reach Supervisor metadata and the health endpoint."""
    config = (ADDON / "config.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*["\']?([^"\'\s]+)', config, re.MULTILINE)
    assert match is not None
    assert match.group(1) == __version__

    changelog = (ADDON / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {__version__}" in changelog


def test_dockerfile_uses_multiarch_home_assistant_base_and_required_modules() -> None:
    dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("FROM ghcr.io/home-assistant/base-debian:trixie\n")
    assert "ARG BUILD_FROM" not in dockerfile
    assert not (ADDON / "build.yaml").exists()
    assert 'io.hass.type="app"' in dockerfile
    assert 'test -f "/usr/lib/baresip/modules/${module}"' in dockerfile
    for module in ("account.so", "aufile.so", "ctrl_tcp.so", "g711.so", "menu.so"):
        assert module in dockerfile


def test_service_script_fails_closed_without_supervisor_options() -> None:
    script = (
        ADDON / "rootfs" / "etc" / "services.d" / "unifi-talk-alarm" / "run"
    ).read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in script
    assert "umask 077" in script
    assert "[[ ! -r /data/options.json ]]" in script


def test_translations_are_valid_json_and_use_exact_talk_field_labels() -> None:
    guided_path = "Talk > Phones > Add Third-Party Device > Overview"

    for language in ("en", "de"):
        translation_file = ADDON / "translations" / f"{language}.json"
        translations = json.loads(translation_file.read_text(encoding="utf-8"))
        configuration = translations["configuration"]

        assert set(configuration) == OPTION_KEYS
        assert list(configuration)[:4] == list(CORE_FIELD_LABELS)
        assert {
            key: configuration[key]["name"] for key in CORE_FIELD_LABELS
        } == CORE_FIELD_LABELS

        for key in CORE_FIELD_LABELS:
            assert guided_path in configuration[key]["description"]

        for entry in configuration.values():
            assert set(entry) == {"name", "description"}
            assert entry["name"].strip()
            assert entry["description"].strip()

        provider_warning = " ".join(
            entry["description"] for entry in configuration.values()
        )
        for provider_field in ("Auth Username", "SIP Proxy", "Realm"):
            assert provider_field in provider_warning

        api_token = configuration["api_token"]
        assert "UniFi Talk" in api_token["description"]
        if language == "en":
            assert "create it yourself" in api_token["name"]
            assert "does not come from UniFi Talk" in api_token["description"]
        else:
            assert "selbst erstellen" in api_token["name"]
            assert "stammt nicht aus UniFi Talk" in api_token["description"]
