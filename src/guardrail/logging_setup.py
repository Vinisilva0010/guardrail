"""Logging configuration shared by every entry point.

structlog's defaults plus the redaction processor, placed after exception
rendering so a traceback that carries a key-bearing URL is masked too.
"""

import structlog

from guardrail.redact import structlog_processor

_configured = False


def configure_logging() -> None:
    """Install the processors once. Safe to call from every entry point."""
    global _configured
    if _configured:
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.processors.format_exc_info,
            structlog_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ]
    )
    _configured = True
