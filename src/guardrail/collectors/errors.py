"""Shared collector exceptions."""


class UpstreamDataError(ValueError):
    """The response was well-formed HTTP but the payload is unusable.

    Distinct from transport errors: re-requesting bad data returns bad data, so
    this is never retried.
    """


class ChecksumMismatch(Exception):
    """A downloaded archive did not match its published SHA256.

    Retryable: a corrupted transfer usually succeeds on a second attempt. Never
    parsed, because silently storing corrupted open interest would poison the
    backtest without raising anything.
    """
