import logging
import sys

import pytest

from devswarm.storage.redact import REPLACEMENT, RedactingLogFilter, Redactor


def test_known_secret_is_removed() -> None:
    assert Redactor(["hunter2hunter2"]).text("pw=hunter2hunter2!") == f"pw={REPLACEMENT}!"


def test_short_values_are_ignored_to_avoid_mangling_text() -> None:
    assert Redactor(["abc"]).text("abcdef") == "abcdef"


def test_longer_secret_wins_over_its_substring() -> None:
    assert Redactor(["secretvalue", "secretvalue-extended"]).text("secretvalue-extended") == (
        REPLACEMENT
    )


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnop_qrstu",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_11ABCDEFG0abcdefghijkl",
        "AKIAABCDEFGHIJKLMNOP",
    ],
)
def test_unknown_secrets_are_caught_by_pattern(secret: str) -> None:
    assert secret not in Redactor().text(f"leaked {secret} here")


def test_from_env_picks_secret_names_and_skips_placeholders() -> None:
    redactor = Redactor.from_env(
        {
            "ANTHROPIC_API_KEY": "real-anthropic-value",
            "GITHUB_TOKEN": "your-github-token-here",
            "HOME": "/home/someone/long",
        }
    )
    text = "real-anthropic-value your-github-token-here /home/someone/long"
    assert redactor.text(text) == f"{REPLACEMENT} your-github-token-here /home/someone/long"


def test_nested_values_and_keys_are_redacted() -> None:
    data = {"outer": [{"token": "topsecretvalue"}], "topsecretvalue": 1}
    assert Redactor(["topsecretvalue"]).value(data) == {
        "outer": [{"token": REPLACEMENT}],
        REPLACEMENT: 1,
    }


def test_log_filter_redacts_args_and_exceptions() -> None:
    try:
        raise ValueError("failed with topsecretvalue")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "x", logging.WARNING, "f.py", 1, "token is %s", ("topsecretvalue",), exc_info
    )
    assert RedactingLogFilter(Redactor(["topsecretvalue"])).filter(record)
    formatted = logging.Formatter().format(record)
    assert "topsecretvalue" not in formatted
    assert REPLACEMENT in formatted
