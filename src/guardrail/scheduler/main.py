"""The scheduler process (APScheduler inside one process).

Run it with `uv run guardrail-scheduler`. One instance only: a Postgres advisory
lock refuses a second one, so two processes can never double-run a job. Killing
and restarting is safe because every collector is idempotent; on start it runs
only the jobs whose data is actually stale, so restarting often does not hammer
the sources.
"""

import argparse
import random
import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Final

import structlog
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Connection, select, text
from sqlalchemy.orm import Session

from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import SourceHealth
from guardrail.db.session import dispose_engine, get_engine, session_scope
from guardrail.scheduler.jobs import all_jobs
from guardrail.scheduler.policy import JobState
from guardrail.scheduler.runner import Job, Recorder, execute

log = structlog.get_logger(__name__)

# Key of the advisory lock that keeps the scheduler to one process.
LOCK_KEY: Final = 7311001
# A job counts as stale when its source is older than cadence times this.
STALE_FACTOR: Final = 1.5
# Gap between start-up catch-up runs, so they do not all start together.
CATCHUP_STAGGER: Final = timedelta(seconds=15)
# A late job still runs if the process was busy for up to this long.
MISFIRE_GRACE: Final = 300
WORKERS: Final = 4
# No job may overlap itself, and runs missed while the process was busy collapse
# into one instead of piling up.
JOB_DEFAULTS: Final = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": MISFIRE_GRACE,
}


def utcnow() -> datetime:
    """The current instant."""
    return datetime.now(UTC)


def trigger_for(job: Job) -> BaseTrigger:
    """The APScheduler trigger of a job."""
    if job.kind == "interval":
        return IntervalTrigger(timezone=UTC, **job.schedule)
    return CronTrigger(timezone=UTC, **job.schedule)


def record_job_health(name: str, ok: bool, error: str | None) -> None:
    """Write a job's outcome to source_health under job.<name>."""
    with session_scope() as session:
        if ok:
            record_success(session, f"job.{name}")
        else:
            record_failure(session, f"job.{name}", error or "failed")


def stale_jobs(session: Session, jobs: list[Job], now: datetime) -> list[Job]:
    """Jobs whose data is old enough to refresh right after start-up.

    A job with no recorded source, or with a source that never succeeded, is
    stale. The heartbeat is excluded: it runs on its own interval.
    """
    rows = list(session.execute(select(SourceHealth)).scalars())
    stale: list[Job] = []
    for job in jobs:
        if job.name == "heartbeat":
            continue
        mine = (
            [r for r in rows if r.source.startswith(job.sources)] if job.sources else []
        )
        if not mine or any(r.last_success_at is None for r in mine):
            stale.append(job)
            continue
        oldest = min(r.last_success_at for r in mine if r.last_success_at is not None)
        if now - oldest > job.cadence * STALE_FACTOR:
            stale.append(job)
    return stale


class Service:
    """Owns the job states and runs jobs on behalf of the scheduler."""

    def __init__(
        self,
        jobs: list[Job],
        scheduler: BaseScheduler | None = None,
        record: Recorder = record_job_health,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.record = record
        self.clock = clock
        self.jobs = {j.name: j for j in jobs}
        self.states = {j.name: JobState() for j in jobs}
        self.scheduler = scheduler
        self.rng = random.Random()

    def run(self, name: str) -> None:
        """Run one job by name. Never raises."""
        job = self.jobs[name]
        result = execute(job, self.states[name], self.clock, self.rng, self.record)
        decision = result.decision
        if decision is None or decision.retry_in is None or self.scheduler is None:
            return
        # A retry later than the next scheduled run would only duplicate it.
        if decision.retry_in >= job.cadence:
            return
        self.scheduler.add_job(
            self.run,
            "date",
            run_date=self.clock() + decision.retry_in,
            args=[name],
            id=f"{name}.retry",
            replace_existing=True,
        )
        log.info(
            "job.retry_scheduled",
            job=name,
            in_seconds=int(decision.retry_in.total_seconds()),
        )


def build_scheduler(service: Service) -> BlockingScheduler:
    """A scheduler with every job registered and no job able to overlap itself."""
    scheduler = BlockingScheduler(
        timezone=UTC,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE,
        },
        executors={"default": {"type": "threadpool", "max_workers": WORKERS}},
    )
    service.scheduler = scheduler
    for job in service.jobs.values():
        scheduler.add_job(service.run, trigger_for(job), args=[job.name], id=job.name)
    return scheduler


def acquire_lock() -> Connection | None:
    """Take the single-instance lock, or return None when another process has it."""
    connection = get_engine().connect()
    got = connection.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}
    ).scalar_one()
    if not got:
        connection.close()
        return None
    return connection


def release_lock(connection: Connection) -> None:
    """Give the lock back. Closing the connection is not enough: it goes back to
    the pool still holding a lock that belongs to the database session."""
    try:
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        connection.close()


def run_once(names: list[str] | None) -> int:
    """Run jobs one after the other, now. Exit status 1 if any failed."""
    jobs = [j for j in all_jobs() if names is None or j.name in names]
    unknown = set(names or []) - {j.name for j in jobs}
    if unknown:
        print(f"unknown jobs: {sorted(unknown)}")
        return 2
    service = Service(jobs)
    failed = 0
    for job in jobs:
        result = execute(
            job, service.states[job.name], utcnow, service.rng, record_job_health
        )
        print(f"{'ok  ' if result.ok else 'FAIL'} {job.name}: {result.detail}")
        failed += 0 if result.ok else 1
    return 1 if failed else 0


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Run the Guardrail scheduler.")
    parser.add_argument(
        "--once", nargs="*", metavar="JOB", help="run jobs now and exit"
    )
    parser.add_argument(
        "--list", action="store_true", help="show the schedule and exit"
    )
    parser.add_argument(
        "--skip", nargs="+", default=[], metavar="JOB", help="jobs to leave out"
    )
    args = parser.parse_args()

    try:
        unknown = set(args.skip) - {j.name for j in all_jobs()}
        if unknown:
            print(f"unknown jobs: {sorted(unknown)}")
            return 2
        if args.list:
            for job in all_jobs():
                print(f"{job.name:<16} {job.kind:<8} {job.schedule}")
            return 0
        if args.once is not None:
            return run_once(args.once or None)

        lock = acquire_lock()
        if lock is None:
            print("another scheduler is already running; exiting")
            return 1

        jobs = [j for j in all_jobs() if j.name not in set(args.skip)]
        service = Service(jobs)
        scheduler = build_scheduler(service)
        with session_scope() as session:
            due = stale_jobs(session, jobs, utcnow())
        start = utcnow()
        for index, job in enumerate(due):
            scheduler.add_job(
                service.run,
                "date",
                run_date=start + CATCHUP_STAGGER * (index + 1),
                args=[job.name],
                id=f"{job.name}.catchup",
            )
        log.info(
            "scheduler.start", jobs=len(service.jobs), catching_up=[j.name for j in due]
        )

        stop = threading.Event()

        def shutdown(signum: int, _frame: FrameType | None) -> None:
            log.info("scheduler.signal", signal=signum)
            stop.set()
            scheduler.shutdown(wait=False)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            scheduler.start()
        finally:
            release_lock(lock)
        return 0
    finally:
        dispose_engine()


if __name__ == "__main__":
    sys.exit(main())
