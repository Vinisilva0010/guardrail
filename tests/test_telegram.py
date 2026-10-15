"""Tests for the Telegram client: what it sends, and that the token never leaks.

The token is in the request path, so the risk is not the message but the error:
an HTTP client's exception text carries the URL.
"""

import json

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr

from guardrail.alerts.telegram import API, MAX_CHARS, TelegramClient, TelegramError

TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-000000"
SEND_URL = f"{API}/bot{TOKEN}/sendMessage"


def _client(http: httpx.AsyncClient) -> TelegramClient:
    return TelegramClient(http, SecretStr(TOKEN), "42")


@pytest.mark.asyncio
@respx.mock
async def test_message_is_sent_as_plain_text_to_the_chat() -> None:
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as http:
        await _client(http).send("ALTO IMPACTO em 1 h: CPI")

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "chat_id": "42",
        "text": "ALTO IMPACTO em 1 h: CPI",
        "disable_web_page_preview": True,
    }
    assert "parse_mode" not in body  # third-party text must carry no formatting


@pytest.mark.asyncio
@respx.mock
async def test_long_text_is_cut_to_what_telegram_accepts() -> None:
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as http:
        await _client(http).send("x" * 10_000)

    assert len(json.loads(route.calls.last.request.content)["text"]) == MAX_CHARS


@pytest.mark.asyncio
@respx.mock
async def test_a_refusal_raises_without_the_token() -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as caught:
            await _client(http).send("hi")

    assert "400" in str(caught.value) and "chat not found" in str(caught.value)
    assert TOKEN not in str(caught.value) and caught.value.retry_after is None


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_reports_how_long_to_wait() -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "ok": False,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as caught:
            await _client(http).send("hi")

    assert caught.value.retry_after == 17


@pytest.mark.asyncio
@respx.mock
async def test_ok_false_with_a_200_status_is_still_a_failure() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": False}))

    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError):
            await _client(http).send("hi")


@pytest.mark.asyncio
@respx.mock
async def test_a_gateway_page_instead_of_json_is_a_failure() -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError, match="HTTP 502"):
            await _client(http).send("hi")


@pytest.mark.asyncio
@respx.mock
async def test_a_transport_error_does_not_carry_the_url_or_chain_to_it() -> None:
    """httpx's own message includes the request URL, token and all."""
    respx.post(SEND_URL).mock(
        side_effect=httpx.ConnectError(f"cannot connect to {SEND_URL}")
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as caught:
            await _client(http).send("hi")

    assert "ConnectError" in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_token_is_hidden_in_the_client_repr() -> None:
    http = httpx.AsyncClient()

    assert TOKEN not in repr(_client(http))


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_send_logged_by_the_caller_leaks_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.post(SEND_URL).mock(
        side_effect=httpx.ConnectError(f"cannot connect to {SEND_URL}")
    )
    log = structlog.get_logger("alerts")

    async with httpx.AsyncClient() as http:
        try:
            await _client(http).send("hi")
        except TelegramError as exc:
            log.error("alerts.send_failed", error=str(exc), exc_info=True)

    out = capsys.readouterr().out
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0" not in out
    assert TOKEN not in out
