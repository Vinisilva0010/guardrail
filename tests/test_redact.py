"""Tests for secret redaction in logs and error text."""

import pytest
import structlog
from structlog.testing import capture_logs

from guardrail.redact import MASK, redact, redact_value, structlog_processor

TELEGRAM_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&apikey=ABC123XYZ&horizon=3month",
            "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&apikey=***&horizon=3month",
        ),
        (
            "https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL&api_key=0123456789abcdef0123456789abcdef&limit=3",
            "https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL&api_key=***&limit=3",
        ),
        ("GET /x?API_KEY=Secret&a=1", "GET /x?API_KEY=***&a=1"),
        ("?token=abc123&b=2", "?token=***&b=2"),
        (
            f"POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            "POST https://api.telegram.org/bot123456789:***/sendMessage",
        ),
        ("Authorization: Bearer abcdefghijklmnop1234", "Authorization: Bearer ***"),
        ("nothing sensitive here", "nothing sensitive here"),
        ("", ""),
    ],
)
def test_redact(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_redact_is_idempotent() -> None:
    once = redact(f"https://x/bot{TELEGRAM_TOKEN}/send?apikey=KEY")

    assert redact(once) == once


def test_a_hash_that_is_not_a_key_is_left_alone() -> None:
    """32 hex characters are also the shape of an event identity key."""
    key = "0123456789abcdef0123456789abcdef"

    assert redact(f"event {key} updated") == f"event {key} updated"


def test_containers_are_walked_and_secret_field_names_are_masked() -> None:
    value = {
        "url": "https://x/q?apikey=K1&y=2",
        "token": "plain",
        "Authorization": "anything",
        "nested": [{"msg": "token=T2"}, ("a", "https://x?secret=S3")],
        "count": 5,
    }

    out = redact_value(value)

    assert out["url"] == "https://x/q?apikey=***&y=2"
    assert out["token"] == MASK and out["Authorization"] == MASK
    assert out["nested"][0]["msg"] == "token=***"
    assert out["nested"][1] == ("a", "https://x?secret=***")
    assert out["count"] == 5


def test_every_log_field_is_masked_by_the_processor() -> None:
    event = {"event": "fetching", "url": "https://x?apikey=K", "api_key": "K", "n": 3}

    out = structlog_processor(None, "info", event)

    assert out["url"] == "https://x?apikey=***"
    assert out["api_key"] == MASK
    assert out["n"] == 3 and out["event"] == "fetching"


def test_the_configured_pipeline_masks_a_real_log_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end through structlog as the entry points configure it."""
    log = structlog.get_logger("test")

    log.info(
        "call",
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        apikey="K",
    )

    out = capsys.readouterr().out
    assert (
        TELEGRAM_TOKEN not in out and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0" not in out
    )
    assert "***" in out


def test_a_traceback_carrying_a_token_is_masked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = structlog.get_logger("test")
    try:
        raise RuntimeError(
            f"failed POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        )
    except RuntimeError:
        log.error("send failed", exc_info=True)

    out = capsys.readouterr().out
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0" not in out
    assert "bot123456789:***" in out


def test_capture_logs_still_sees_events() -> None:
    """The tests that read logs elsewhere must keep working."""
    with capture_logs() as logs:
        structlog.get_logger("x").info("hello", n=1)

    assert logs[0]["event"] == "hello"
