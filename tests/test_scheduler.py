"""Tests for the scheduler: failure policy, the safe runner, the schedule itself.

Backoff arithmetic, by hand. base = 30 s, cap = 15 min = 900 s, jitter factor in
[0.5, 1.0]. After the n-th consecutive failure the raw wait is min(900, 30 * 2^(n-1)):

    n=1   30 s     n=2   60 s     n=3  120 s     n=4  240 s
    n=5  480 s     n=6  900 s (960 capped)         n>=6 900 s

and the actual wait is the raw wait times the factor.
"""

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session

from guardrail.db.models import SourceHealth
from guardrail.scheduler import policy
from guardrail.scheduler.jobs import _from_errors, all_jobs
from guardrail.scheduler.main import (
    JOB_DEFAULTS,
    LOCK_KEY,
    Service,
    acquire_lock,
    build_scheduler,
    release_lock,
    stale_jobs,
    trigger_for,
)
from guardrail.scheduler.policy import (
    JobState,
    backoff_delay,
    is_open,
    on_failure,
    on_success,
)
from guardrail.scheduler.runner import Job, Outcome, execute

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # Thursday
S = timedelta(seconds=1)
BASE, CAP = policy.BACKOFF_BASE, policy.BACKOFF_CAP


class Fixed(random.Random):
    """A generator whose uniform() always returns the same factor."""

    def __init__(self, factor: float) -> None:
        super().__init__(0)
        self.factor = factor

    def uniform(self, a: float, b: float) -> float:
        return self.factor


# --- backoff -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "raw_seconds"),
    [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 900), (7, 900), (50, 900)],
)
def test_backoff_doubles_up_to_the_cap(failures: int, raw_seconds: int) -> None:
    assert backoff_delay(failures, BASE, CAP, Fixed(1.0)) == raw_seconds * S
    assert backoff_delay(failures, BASE, CAP, Fixed(0.5)) == raw_seconds * S / 2


def test_backoff_survives_a_job_that_fails_for_weeks() -> None:
    """Hourly attempts for a month is 720 failures; 2 ** 720 must not overflow."""
    assert backoff_delay(720, BASE, CAP, Fixed(1.0)) == CAP
    assert backoff_delay(10**6, BASE, CAP, Fixed(1.0)) == CAP


def test_backoff_needs_a_failure() -> None:
    with pytest.raises(ValueError):
        backoff_delay(0, BASE, CAP, random.Random(1))


@given(st.integers(1, 1000), st.integers(0, 10_000))
def test_backoff_stays_inside_the_jitter_band(failures: int, seed: int) -> None:
    raw = min(CAP, BASE * (1 << min(failures - 1, policy.MAX_EXPONENT)))

    delay = backoff_delay(failures, BASE, CAP, random.Random(seed))

    assert raw * 0.5 <= delay <= raw


def test_jitter_spreads_jobs_that_failed_together() -> None:
    delays = {backoff_delay(3, BASE, CAP, random.Random(seed)) for seed in range(20)}

    assert len(delays) > 10


# --- circuit breaker -----------------------------------------------------------


def test_failures_below_the_threshold_schedule_a_retry() -> None:
    state = JobState()
    for n in range(1, policy.CIRCUIT_AFTER):
        decision = on_failure(state, NOW, "boom", Fixed(1.0))
        assert decision.retry_in is not None and not decision.circuit_opened
        assert state.failures == n
    assert not is_open(state, NOW)


def test_the_fifth_consecutive_failure_opens_the_circuit_for_an_hour() -> None:
    state = JobState()
    for _ in range(policy.CIRCUIT_AFTER - 1):
        on_failure(state, NOW, "boom", Fixed(1.0))

    decision = on_failure(state, NOW, "boom", Fixed(1.0))

    assert decision.circuit_opened and decision.retry_in is None
    assert state.open_until == NOW + timedelta(hours=1)
    assert is_open(state, NOW + timedelta(minutes=59))
    assert not is_open(state, NOW + timedelta(hours=1))


def test_half_open_attempt_that_fails_reopens_at_once() -> None:
    state = JobState(failures=policy.CIRCUIT_AFTER, open_until=NOW)
    later = NOW + timedelta(hours=1, minutes=1)

    decision = on_failure(state, later, "still down", Fixed(1.0))

    assert decision.circuit_opened
    assert state.open_until == later + timedelta(hours=1)


def test_a_success_closes_the_circuit_and_forgets_the_failures() -> None:
    state = JobState(failures=7, open_until=NOW + timedelta(hours=1), last_error="x")

    on_success(state, NOW)

    assert (state.failures, state.open_until, state.last_error) == (0, None, None)
    assert state.last_success_at == NOW
    assert not is_open(state, NOW)


def test_a_success_in_between_restarts_the_count() -> None:
    state = JobState()
    for _ in range(4):
        on_failure(state, NOW, "boom", Fixed(1.0))
    on_success(state, NOW)

    on_failure(state, NOW, "boom", Fixed(1.0))

    assert state.failures == 1 and not is_open(state, NOW)


# --- the safe runner -----------------------------------------------------------


class Recorded:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []

    def __call__(self, name: str, ok: bool, error: str | None) -> None:
        self.calls.append((name, ok, error))


def _counting(counter: list[int], outcome: Outcome | None = None) -> Any:
    """A job that counts how many times it ran."""

    def run() -> Outcome:
        counter.append(1)
        return outcome or Outcome(True)

    return run


def _job(run: Any, cadence: timedelta = timedelta(hours=6), **extra: Any) -> Job:
    return Job("demo", run, "interval", {"hours": 6}, cadence, **extra)


def _go(
    job: Job, state: JobState | None = None, record: Any = None
) -> tuple[Any, JobState, Recorded]:
    state = state or JobState()
    recorded = record or Recorded()
    result = execute(job, state, lambda: NOW, Fixed(1.0), recorded)
    return result, state, recorded


def test_a_job_that_raises_is_a_failure_not_a_crash() -> None:
    def boom() -> Outcome:
        raise RuntimeError("boom")

    result, state, recorded = _go(_job(boom))

    assert (result.ran, result.ok) == (True, False)
    assert state.failures == 1 and state.last_error == "RuntimeError: boom"
    assert recorded.calls == [("demo", False, "RuntimeError: boom")]


def test_a_job_that_reports_failure_is_a_failure() -> None:
    result, state, recorded = _go(_job(lambda: Outcome(False, "429 from source")))

    assert not result.ok and state.failures == 1
    assert recorded.calls == [("demo", False, "429 from source")]


def test_a_job_that_works_is_recorded_and_clears_the_failures() -> None:
    state = JobState(failures=3)

    result, state, recorded = _go(_job(lambda: Outcome(True, "5 rows")), state)

    assert result.ok and state.failures == 0
    assert recorded.calls == [("demo", True, None)]


def test_an_open_circuit_skips_the_job_without_running_it() -> None:
    ran: list[int] = []
    state = JobState(failures=5, open_until=NOW + timedelta(minutes=30))

    result, _, recorded = _go(_job(_counting(ran)), state)

    assert (result.ran, ran, recorded.calls) == (False, [], [])


def test_failing_to_record_health_does_not_take_the_job_down() -> None:
    def broken(*_: Any) -> None:
        raise RuntimeError("database is down")

    result, state, _ = _go(_job(lambda: Outcome(True)), record=broken)

    assert result.ok and state.failures == 0


def test_an_interrupt_is_not_swallowed() -> None:
    def stop() -> Outcome:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _go(_job(stop))


def test_the_error_text_kept_for_health_is_bounded() -> None:
    result, _, recorded = _go(_job(lambda: Outcome(False, "x" * 5000)))

    assert not result.ok
    assert len(recorded.calls[0][2] or "") == 500


def test_the_job_s_own_backoff_applies() -> None:
    job = _job(
        lambda: Outcome(False, "429"),
        backoff_base=timedelta(minutes=30),
        backoff_cap=timedelta(hours=3),
    )

    result, _, _ = _go(job)

    assert result.decision is not None and result.decision.retry_in == timedelta(
        minutes=30
    )


def test_error_aggregation_for_collectors_that_return_errors() -> None:
    assert _from_errors([("a", None), ("b", None)], "ok").ok
    mixed = _from_errors([("a", None), ("b", "429"), ("c", "timeout")], "ok")
    assert not mixed.ok and mixed.detail == "b: 429; c: timeout"


# --- the service: retries ------------------------------------------------------


class StubScheduler:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add_job(self, func: Any, trigger: str, **kwargs: Any) -> None:
        self.added.append({"trigger": trigger, **kwargs})


def _service(job: Job) -> tuple[Service, StubScheduler]:
    stub = StubScheduler()
    service = Service([job], scheduler=stub, record=Recorded(), clock=lambda: NOW)
    service.rng = Fixed(1.0)
    return service, stub


def test_a_failed_run_schedules_one_retry_under_a_stable_id() -> None:
    service, stub = _service(_job(lambda: Outcome(False, "boom")))

    service.run("demo")
    service.run("demo")

    assert [a["id"] for a in stub.added] == ["demo.retry", "demo.retry"]
    assert all(a["replace_existing"] is True for a in stub.added)
    # 1st failure: 30 s. 2nd: 60 s. With the jitter factor fixed at 1.0.
    assert [a["run_date"] - NOW for a in stub.added] == [30 * S, 60 * S]


def test_a_retry_later_than_the_next_scheduled_run_is_not_scheduled() -> None:
    job = _job(lambda: Outcome(False, "boom"), cadence=timedelta(seconds=20))
    service, stub = _service(job)

    service.run("demo")

    assert stub.added == []


def test_an_opened_circuit_schedules_no_retry_and_blocks_further_runs() -> None:
    calls: list[int] = []

    def fail() -> Outcome:
        calls.append(1)
        return Outcome(False, "down")

    service, stub = _service(_job(fail))
    for _ in range(policy.CIRCUIT_AFTER + 3):
        service.run("demo")

    assert len(calls) == policy.CIRCUIT_AFTER
    assert len(stub.added) == policy.CIRCUIT_AFTER - 1


def test_one_failing_job_does_not_stop_the_others() -> None:
    def boom() -> Outcome:
        raise RuntimeError("boom")

    good_runs: list[int] = []
    jobs = [
        Job("bad", boom, "interval", {"hours": 1}, timedelta(hours=1)),
        Job(
            "good",
            _counting(good_runs),
            "interval",
            {"hours": 1},
            timedelta(hours=1),
        ),
    ]
    service = Service(
        jobs, scheduler=StubScheduler(), record=Recorded(), clock=lambda: NOW
    )

    service.run("bad")
    service.run("good")

    assert good_runs == [1]


# --- the schedule --------------------------------------------------------------


def test_every_job_has_a_unique_name_and_a_working_trigger() -> None:
    jobs = all_jobs()

    assert len({j.name for j in jobs}) == len(jobs)
    for job in jobs:
        assert trigger_for(job) is not None


def test_jobs_cannot_overlap_themselves_and_late_runs_are_coalesced() -> None:
    scheduler = build_scheduler(Service(all_jobs()))

    assert JOB_DEFAULTS["max_instances"] == 1 and JOB_DEFAULTS["coalesce"] is True
    # APScheduler applies job defaults when it starts, so before that they are
    # visible on the scheduler itself.
    assert scheduler._job_defaults == JOB_DEFAULTS
    assert {j.id for j in scheduler.get_jobs()} == {j.name for j in all_jobs()}


def _next(name: str, after: datetime = NOW) -> datetime:
    job = next(j for j in all_jobs() if j.name == name)
    fire: datetime = trigger_for(job).get_next_fire_time(None, after)
    return fire


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Thursday 15 Oct 2026, 14:00 UTC. Cron fields worked out by hand.
        ("klines_1h", datetime(2026, 10, 15, 14, 2, tzinfo=UTC)),
        ("klines_4h", datetime(2026, 10, 15, 16, 3, tzinfo=UTC)),  # 0,4,8,12,16,20
        ("funding", datetime(2026, 10, 15, 16, 20, tzinfo=UTC)),
        ("klines_1d", datetime(2026, 10, 16, 0, 6, tzinfo=UTC)),
        ("us_bars", datetime(2026, 10, 16, 5, 30, tzinfo=UTC)),
        ("us_actions", datetime(2026, 10, 16, 1, 10, tzinfo=UTC)),
        ("vision_metrics", datetime(2026, 10, 16, 2, 30, tzinfo=UTC)),
        ("impact_stats", datetime(2026, 10, 16, 3, 30, tzinfo=UTC)),
        ("audit", datetime(2026, 10, 16, 4, 0, tzinfo=UTC)),
        ("us_holidays", datetime(2026, 10, 16, 6, 0, tzinfo=UTC)),
        ("calendar_rules", datetime(2026, 10, 16, 6, 10, tzinfo=UTC)),
        (
            "bcb",
            datetime(2026, 10, 16, 11, 15, tzinfo=UTC),
        ),  # 11:15 already passed today
        ("b3_close", datetime(2026, 10, 16, 22, 30, tzinfo=UTC)),  # Friday
    ],
)
def test_cron_jobs_fire_when_the_plan_says(name: str, expected: datetime) -> None:
    assert _next(name) == expected


def test_b3_closes_on_tuesday_and_friday_only() -> None:
    fires: list[str] = []
    cursor = NOW
    for _ in range(4):
        cursor = _next("b3_close", cursor + timedelta(minutes=1))
        fires.append(cursor.strftime("%a %H:%M"))

    assert fires == ["Fri 22:30", "Tue 22:30", "Fri 22:30", "Tue 22:30"]


def test_b3_stays_inside_the_monthly_request_ceiling() -> None:
    """600 tickers per run, twice a week: about 5,200 of the 15,000 a month."""
    runs_per_month = 52 * 2 / 12

    assert 600 * runs_per_month < 0.4 * 15_000


@pytest.mark.parametrize(
    ("name", "seconds"),
    [
        ("heartbeat", 60),
        ("macro_calendar", 6 * 3600),
        ("us_news", 1800),
        ("rss_news", 1800),
        ("themes", 1800),
        ("open_interest", 900),
    ],
)
def test_interval_jobs_run_at_the_planned_cadence(name: str, seconds: int) -> None:
    job = next(j for j in all_jobs() if j.name == name)

    assert trigger_for(job).interval == timedelta(seconds=seconds)


def test_the_rate_limited_calendar_backs_off_for_half_an_hour() -> None:
    job = next(j for j in all_jobs() if j.name == "macro_calendar")

    assert job.backoff_base == timedelta(minutes=30)


# --- start-up catch-up ---------------------------------------------------------


def _blank_health(session: Session) -> None:
    session.execute(text("TRUNCATE source_health"))


def test_with_no_history_everything_but_the_heartbeat_is_stale(
    session: Session,
) -> None:
    _blank_health(session)

    stale = {j.name for j in stale_jobs(session, all_jobs(), NOW)}

    assert stale == {j.name for j in all_jobs()} - {"heartbeat"}


def test_fresh_sources_are_not_refetched_on_restart(session: Session) -> None:
    _blank_health(session)
    session.add_all(
        [
            SourceHealth(
                source="forexfactory.calendar",
                last_success_at=NOW - timedelta(hours=2),
                consecutive_failures=0,
            ),
            SourceHealth(
                source="alpaca.news",
                last_success_at=NOW - timedelta(minutes=10),
                consecutive_failures=0,
            ),
        ]
    )
    session.flush()

    stale = {j.name for j in stale_jobs(session, all_jobs(), NOW)}

    assert "macro_calendar" not in stale and "us_news" not in stale
    assert "rss_news" in stale


def test_a_source_older_than_one_and_a_half_cadences_is_stale(session: Session) -> None:
    _blank_health(session)
    session.add(
        SourceHealth(
            source="forexfactory.calendar",
            last_success_at=NOW - timedelta(hours=9, minutes=1),
            consecutive_failures=0,
        )
    )
    session.flush()

    assert "macro_calendar" in {j.name for j in stale_jobs(session, all_jobs(), NOW)}


def test_a_source_that_never_succeeded_is_stale(session: Session) -> None:
    _blank_health(session)
    session.add(
        SourceHealth(
            source="forexfactory.calendar",
            last_success_at=None,
            consecutive_failures=2,
            last_error="429",
        )
    )
    session.flush()

    assert "macro_calendar" in {j.name for j in stale_jobs(session, all_jobs(), NOW)}


def test_the_oldest_symbol_decides_for_a_multi_symbol_source(session: Session) -> None:
    _blank_health(session)
    session.add_all(
        [
            SourceHealth(
                source="binance.klines.BTCUSDT.1h",
                last_success_at=NOW,
                consecutive_failures=0,
            ),
            SourceHealth(
                source="binance.klines.ETHUSDT.1h",
                last_success_at=NOW - timedelta(days=2),
                consecutive_failures=0,
            ),
        ]
    )
    session.flush()

    assert "klines_1h" in {j.name for j in stale_jobs(session, all_jobs(), NOW)}


# --- one instance only ---------------------------------------------------------


def test_a_second_scheduler_cannot_take_the_lock() -> None:
    first = acquire_lock()
    assert first is not None
    try:
        assert acquire_lock() is None
    finally:
        release_lock(first)

    again = acquire_lock()
    assert again is not None
    release_lock(again)
    assert LOCK_KEY > 0
