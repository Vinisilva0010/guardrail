"""Run one job safely.

A job may raise, hang, or report failure in its own way. None of that may stop
the process or the other jobs: every outcome is turned into success or failure,
recorded, and handed to the policy. Only BaseException subclasses that mean "stop"
(Ctrl-C, interpreter exit) get through.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import structlog

from guardrail.scheduler import policy
from guardrail.scheduler.policy import Decision, JobState

log = structlog.get_logger(__name__)

# Longest error text kept in source_health.
MAX_ERROR_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a job reports about one run."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Job:
    """A scheduled task."""

    name: str
    run: Callable[[], Outcome]
    kind: str  # "interval" or "cron"
    schedule: dict[str, int | str]
    cadence: timedelta
    # source_health keys (prefixes) this job keeps fresh; used to catch up at
    # start-up without refetching what is already current.
    sources: tuple[str, ...] = ()
    backoff_base: timedelta = policy.BACKOFF_BASE
    backoff_cap: timedelta = policy.BACKOFF_CAP


# (job name, ok, detail) -> None. The scheduler wires it to source_health.
Recorder = Callable[[str, bool, str | None], None]


@dataclass(frozen=True, slots=True)
class RunResult:
    """What happened when a job was asked to run."""

    ran: bool
    ok: bool
    detail: str
    decision: Decision | None = None


def execute(
    job: Job,
    state: JobState,
    now: Callable[[], datetime],
    rng: random.Random,
    record: Recorder,
) -> RunResult:
    """Run `job` once, honouring its circuit, and update its state."""
    if policy.is_open(state, now()):
        log.warning(
            "job.skipped_circuit_open", job=job.name, until=str(state.open_until)
        )
        return RunResult(False, False, "circuit open")

    try:
        outcome = job.run()
        ok, detail = outcome.ok, outcome.detail
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
        log.error("job.raised", job=job.name, error=detail)

    if ok:
        policy.on_success(state, now())
        _safe_record(record, job.name, True, None)
        log.info("job.ok", job=job.name, detail=detail)
        return RunResult(True, True, detail)

    decision = policy.on_failure(
        state,
        now(),
        detail,
        rng,
        base=job.backoff_base,
        cap=job.backoff_cap,
    )
    _safe_record(record, job.name, False, detail[:MAX_ERROR_CHARS])
    log.warning(
        "job.failed",
        job=job.name,
        detail=detail,
        failures=state.failures,
        circuit_opened=decision.circuit_opened,
    )
    return RunResult(True, False, detail, decision)


def _safe_record(record: Recorder, name: str, ok: bool, error: str | None) -> None:
    """Recording health must not be able to take the job down: if the database is
    the thing that failed, there is nowhere to write and the log is the record."""
    try:
        record(name, ok, error)
    except Exception as exc:
        log.error("job.record_failed", job=name, error=str(exc))
