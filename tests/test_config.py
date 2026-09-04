"""Configuration safety tests."""

from __future__ import annotations

import pytest

from talk_alarm.config import AppConfig


@pytest.mark.parametrize(
    "number",
    [
        "110",
        "1 1 2",
        "*911",
        "#999",
        "+49110",
        "+49112",
        "0049110",
        "0049112",
    ],
)
def test_emergency_numbers_are_hard_blocked(app_config: AppConfig, number: str) -> None:
    normalized = number.replace(" ", "")
    assert not app_config.number_policy.allows(normalized)


def test_block_rule_wins_over_allow_rule(options: dict) -> None:
    options["allowed_number_rules"] = ["*"]
    options["blocked_number_rules"] = ["prefix:123"]
    config = AppConfig.from_mapping(options)
    assert not config.number_policy.allows("12345")
    assert config.number_policy.allows("150")


@pytest.mark.parametrize("password", ['bad;value', 'bad"value', "bad\\value"])
def test_baresip_account_delimiters_are_rejected(options: dict, password: str) -> None:
    options["sip_password"] = password
    with pytest.raises(ValueError, match="syntax delimiters"):
        AppConfig.from_mapping(options)


def test_short_api_tokens_are_rejected(options: dict) -> None:
    options["api_token"] = "too-short"
    with pytest.raises(ValueError, match="32 to 512"):
        AppConfig.from_mapping(options)
