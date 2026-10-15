"""Telegram, over plain HTTPS. No SDK, send only.

The bot token is part of the request path, so anything that prints the URL prints
the token. Every failure here is therefore re-raised as a TelegramError built
from facts that cannot contain it (a status code, Telegram's own description),
never from the exception text of the HTTP client, and without chaining the
original exception whose message would carry the URL.

Messages are sent as plain text, no parse_mode: a headline or an event title is
third-party text and must not be able to inject formatting or links.
"""

from dataclasses import dataclass
from typing import Final

import httpx
from pydantic import SecretStr

API: Final = "https://api.telegram.org"
# Telegram refuses longer messages.
MAX_CHARS: Final = 4096


class TelegramError(Exception):
    """A message could not be delivered. The text never contains the token."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TelegramClient:
    """Sends messages to one chat."""

    http: httpx.AsyncClient
    token: SecretStr
    chat_id: str

    async def send(self, text: str) -> None:
        """Deliver one text message, or raise TelegramError."""
        url = f"{API}/bot{self.token.get_secret_value()}/sendMessage"
        body = {
            "chat_id": self.chat_id,
            "text": text[:MAX_CHARS],
            "disable_web_page_preview": True,
        }
        try:
            response = await self.http.post(url, json=body)
        except httpx.HTTPError as exc:
            # type(exc).__name__ only: str(exc) can include the URL.
            raise TelegramError(f"transport error: {type(exc).__name__}") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 200 and payload.get("ok") is True:
            return
        description = str(payload.get("description", ""))[:200]
        retry = payload.get("parameters", {}).get("retry_after")
        raise TelegramError(
            f"telegram refused the message: HTTP {response.status_code} "
            f"{description}".strip(),
            retry_after=retry if isinstance(retry, int) else None,
        )
