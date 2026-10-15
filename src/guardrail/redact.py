"""Keep secrets out of logs and error messages (PLANO_ANTECIPACAO §7).

Several sources take the key in the URL (Alpha Vantage, FRED) and the Telegram
token is part of the request path. A key in a URL leaks through request logs,
through exception messages from the HTTP client and through recorded fixtures,
and the repository is public.

`redact` is applied to every string a log event carries, to rendered tracebacks,
and to the error text stored in source_health.
"""

import re
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

MASK: Final = "***"

# name=value in a query string or form, for the parameter names that carry keys.
_PARAM: Final = re.compile(
    r"(?i)((?:\?|&|\b)(?:api[_-]?key|apikey|access[_-]?token|token|secret|password)=)"
    r"[^&\s'\"<>]+"
)
# Telegram bot token inside a URL path: /bot123456789:AA...../sendMessage
_TELEGRAM: Final = re.compile(r"(bot\d{6,}:)[A-Za-z0-9_-]{20,}")
# Authorization header values.
_BEARER: Final = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}")

# Field names whose value is hidden whatever it looks like.
SECRET_FIELDS: Final = frozenset(
    {
        "apikey",
        "api_key",
        "api-key",
        "token",
        "secret",
        "password",
        "authorization",
        "bot_token",
        "telegram_bot_token",
        "apca-api-key-id",
        "apca-api-secret-key",
    }
)


def redact(text: str) -> str:
    """The text with key parameters, bot tokens and bearer values masked."""
    text = _PARAM.sub(lambda m: m.group(1) + MASK, text)
    text = _TELEGRAM.sub(lambda m: m.group(1) + MASK, text)
    return _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)


def redact_value(value: Any) -> Any:
    """Redact strings inside a log value, recursively through containers."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: MASK if str(k).lower() in SECRET_FIELDS else redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def structlog_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor: mask secrets in every field of an event."""
    return {
        key: MASK if key.lower() in SECRET_FIELDS else redact_value(value)
        for key, value in event_dict.items()
    }
