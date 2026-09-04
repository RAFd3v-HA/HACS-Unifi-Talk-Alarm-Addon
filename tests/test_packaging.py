"""Static packaging checks for the standalone Home Assistant add-on."""

from pathlib import Path

from talk_alarm.__main__ import API_BIND_HOST


ROOT = Path(__file__).parents[1]
ADDON = ROOT / "unifi_talk_alarm"


def test_runtime_api_is_loopback_only() -> None:
    assert API_BIND_HOST == "127.0.0.1"


def test_dockerfile_uses_multiarch_home_assistant_base_and_required_modules() -> None:
    dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("FROM ghcr.io/home-assistant/base-debian:trixie\n")
    assert "ARG BUILD_FROM" not in dockerfile
    assert not (ADDON / "build.yaml").exists()
    assert 'io.hass.type="app"' in dockerfile
    for module in ("account.so", "aufile.so", "ctrl_tcp.so", "g711.so", "menu.so"):
        assert module in dockerfile


def test_service_script_fails_closed_without_supervisor_options() -> None:
    script = (
        ADDON / "rootfs" / "etc" / "services.d" / "unifi-talk-alarm" / "run"
    ).read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in script
    assert "umask 077" in script
    assert "[[ ! -r /data/options.json ]]" in script
