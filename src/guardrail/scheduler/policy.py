"""When a failing job may try again, and when it must stop trying.

Pure functions over a small state record: no clock, no randomness of their own
and no I/O, so every rule can be tested with a fixed time and a seeded generator.

* Backoff: after the n-th consecutive failure, wait min(cap, base * 2^(n-1))
  scaled by a random factor in [0.5, 1.0]. The jitter keeps several jobs that
  failed together (one outage) from retrying in the same second.
* Circuit breaker: after `circuit_after` consecutive failures the job stops
  running for `cooldown`. When the cooldown ends the next attempt goes through
  alone ("half open"): success closes the circuit, failure reopens it at once,
  because the failure count was never reset.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

BACKOFF_BASE: Final = timedelta(seconds=30)
BACKOFF_CAP: Final = timedelta(minutes=15)
CIRCUIT_AFTER: Final = 5
CIRCUIT_COOLDOWN: Final = timedelta(hours=1)

MIN_JITTER: Final = 0.5
MAX_EXPONENT: Final = 20


@dataclass(slots=True)
class JobState:
    """What the scheduler remembers about one job between runs."""

    failures: int = 0
    open_until: datetime | None = None
    last_error: str | None = None
    last_success_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do after a failure."""

    retry_in: timedelta | None
    circuit_opened: bool


def backoff_delay(
    failures: int, base: timedelta, cap: timedelta, rng: random.Random
) -> timedelta:
    """Delay before the next attempt after `failures` consecutive failures."""
    if failures < 1:
        raise ValueError("backoff needs at least one failure")
    # The exponent is clamped: a job failing every hour for weeks would otherwise
    # overflow the multiplication long after the cap made it irrelevant.
    raw = min(cap, base * (1 << min(failures - 1, MAX_EXPONENT)))
    factor: float = rng.uniform(MIN_JITTER, 1.0)
    return raw * factor


def is_open(state: JobState, now: datetime) -> bool:
    """Whether the circuit currently blocks runs."""
    return state.open_until is not None and now < state.open_until


def on_success(state: JobState, now: datetime) -> None:
    """A run worked: forget the failures and close the circuit."""
    state.failures = 0
    state.open_until = None
    state.last_error = None
    state.last_success_at = now


def on_failure(
    state: JobState,
    now: datetime,
    error: str,
    rng: random.Random,
    *,
    base: timedelta = BACKOFF_BASE,
    cap: timedelta = BACKOFF_CAP,
    circuit_after: int = CIRCUIT_AFTER,
    cooldown: timedelta = CIRCUIT_COOLDOWN,
) -> Decision:
    """A run failed: count it, open the circuit or schedule a retry."""
    state.failures += 1
    state.last_error = error
    if state.failures >= circuit_after:
        state.open_until = now + cooldown
        return Decision(None, True)
    return Decision(backoff_delay(state.failures, base, cap, rng), False)
