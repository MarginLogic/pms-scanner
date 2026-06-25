"""Per-(machine, environment) APScheduler jobs (004 US3).

One ``CronTrigger`` job per *enabled* environment on this machine, at
``second=<offset>``, ``minute='*'`` with ``max_instances=1`` +
``coalesce=True`` + ``misfire_grace_time=30`` (research.md §2): cross-env
concurrency (FR-006a) via a thread pool; same-env overlap suppression
(FR-006b) via coalescing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.events import (  # type: ignore[import-untyped,unused-ignore]
    EVENT_JOB_MAX_INSTANCES,
)
from apscheduler.executors.pool import (  # type: ignore[import-untyped,unused-ignore]
    ThreadPoolExecutor,
)
from apscheduler.schedulers.background import (  # type: ignore[import-untyped,unused-ignore]
    BackgroundScheduler,
)
from apscheduler.triggers.cron import (  # type: ignore[import-untyped,unused-ignore]
    CronTrigger,
)
from apscheduler.triggers.date import (  # type: ignore[import-untyped,unused-ignore]
    DateTrigger,
)
from apscheduler.triggers.interval import (  # type: ignore[import-untyped,unused-ignore]
    IntervalTrigger,
)

if TYPE_CHECKING:
    from .config import AppSettings
    from .state import BatchRunState

logger = logging.getLogger("scanner.scheduler")

# Machine-level (not per-env) job that zeroes the day-scoped dashboard
# counters at local midnight. Fired by the same in-process BackgroundScheduler
# as the per-env polls; uses the scheduler's default (local) timezone so the
# rollover lines up with the operator's wall clock.
DAILY_RESET_TRIGGER_KWARGS = {"hour": 0, "minute": 0, "second": 0}


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One APScheduler registration for an (machine, env) pair."""

    job_id: str
    env_name: str
    trigger_kwargs: dict[str, Any]
    max_instances: int = 1
    coalesce: bool = True
    misfire_grace_time: int = 30


def build_jobs(settings: AppSettings) -> list[JobSpec]:
    """One JobSpec per ENABLED environment on this machine (FR-006/006b)."""
    machine = settings.machine.name
    return [
        JobSpec(
            job_id=f"{machine}:{env.name}",
            env_name=env.name,
            trigger_kwargs={
                "second": env.schedule_offset_seconds,
                "minute": "*",
            },
        )
        for env in settings.enabled_environments
    ]


@dataclass(slots=True)
class Scheduler:
    """Owns the BackgroundScheduler and the per-env job registrations."""

    settings: AppSettings
    state: BatchRunState | None = None
    run_env: Callable[[str], None] | None = None
    _scheduler: BackgroundScheduler = field(init=False)

    def __post_init__(self) -> None:
        n = max(1, len(self.settings.enabled_environments))
        pool = max(4, 2 * n)
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=pool)}
        )
        if self.run_env is None:
            self.run_env = self._default_run_env
        self._scheduler.add_listener(
            self._on_max_instances, EVENT_JOB_MAX_INSTANCES
        )

    # -- job action ------------------------------------------------------

    def _default_run_env(self, env_name: str) -> None:
        from .batch import BatchRunner
        from .state import app_state

        assert self.state is not None
        env = next(
            e for e in self.settings.environments if e.name == env_name
        )
        BatchRunner(
            env,
            self.settings.machine,
            self.state,
            settle_seconds=self.settings.file_settle_seconds,
            upload_timeout_seconds=self.settings.upload_timeout_seconds,
            upload_max_retries=self.settings.upload_max_retries,
            upload_retry_max_wait_seconds=(
                self.settings.upload_retry_max_wait_seconds
            ),
            # Mirror the dashboard's manual /run path: scheduled runs MUST
            # emit SSE events too, or an open dashboard never refreshes
            # (it only re-fetches /status on load and per SSE event).
            emit=app_state.emit_event,
        ).run_once()

    def _dispatch(self, env_name: str) -> None:
        assert self.run_env is not None
        logger.info(
            "[machine=%s env=%s] scheduled poll firing",
            self.settings.machine.name,
            env_name,
        )
        self.run_env(env_name)

    @property
    def daily_reset_job_id(self) -> str:
        return f"{self.settings.machine.name}:daily-reset"

    def _daily_reset(self) -> None:
        """Zero the day-scoped counters and nudge open dashboards to refresh."""
        from .state import app_state

        assert self.state is not None
        self.state.reset_daily()
        logger.info(
            "[machine=%s] daily counter reset — files/pages/errors zeroed "
            "for all envs",
            self.settings.machine.name,
        )
        app_state.emit_event(
            {"type": "counters_reset", "machine": self.settings.machine.name}
        )

    def _on_max_instances(self, event: Any) -> None:
        logger.info(
            "[machine=%s] job %s skipped — previous run still in progress; "
            "coalesced into at most one queued follow-up (FR-006b)",
            self.settings.machine.name,
            getattr(event, "job_id", "?"),
        )

    # -- registration / lifecycle ---------------------------------------

    def register(
        self,
        *,
        immediate: bool = False,
        interval_seconds: float | None = None,
    ) -> None:
        specs = build_jobs(self.settings)
        for spec in specs:
            if interval_seconds is not None:
                trigger = IntervalTrigger(seconds=interval_seconds)
            elif immediate:
                trigger = DateTrigger(
                    run_date=datetime.now() + timedelta(seconds=0.2)
                )
            else:
                trigger = CronTrigger(**spec.trigger_kwargs)
            self._scheduler.add_job(
                self._dispatch,
                trigger=trigger,
                args=[spec.env_name],
                id=spec.job_id,
                name=spec.job_id,
                max_instances=spec.max_instances,
                coalesce=spec.coalesce,
                misfire_grace_time=spec.misfire_grace_time,
                replace_existing=True,
            )
        self._register_daily_reset()

    def _register_daily_reset(self) -> None:
        """Register the machine-level midnight counter-reset job (FR-005).

        Skipped when no ``state`` is wired (e.g. scheduler unit tests that
        only exercise the per-env poll dispatch) — there is nothing to reset.
        A misfire grace of one hour lets the reset still run if the process
        was briefly asleep/down across midnight; longer outages simply skip
        the cron fire (in-memory counters are already zero after a restart).
        """
        if self.state is None:
            return
        self._scheduler.add_job(
            self._daily_reset,
            trigger=CronTrigger(**DAILY_RESET_TRIGGER_KWARGS),
            id=self.daily_reset_job_id,
            name=self.daily_reset_job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)

    # Legacy signal-handler compatibility (__main__._shutdown).
    def shutdown(self, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)
