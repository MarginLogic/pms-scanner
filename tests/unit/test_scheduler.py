"""Unit tests for scanner/scheduler.py — T031 + T033 (004 US3).

build_jobs returns one CronTrigger spec per ENABLED env on this machine
with second=offset, minute='*', max_instances=1, coalesce=True,
misfire_grace_time=30 (FR-006/006b). Concurrency: two jobs firing in
the same second are serviced by two distinct worker threads (FR-006a).
"""
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

from config import load_settings
from scheduler import JobSpec, Scheduler, build_jobs
from state import BatchRunState, app_state


def _settings(tmp_path: Path, *, staging_enabled: bool = True):
    p = tmp_path / "p"
    s = tmp_path / "s"
    p.mkdir()
    s.mkdir()
    env = {
        "MACHINE_IDENTITY": "macmini",
        "NTP__STARTUP_REQUIRED": "false",
        "ENVIRONMENTS": "production,staging",
        "ENV_PRODUCTION__WATCH_DIR": str(p),
        "ENV_PRODUCTION__API_TOKEN": "pt",
        "ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS": "0",
        "ENV_STAGING__WATCH_DIR": str(s),
        "ENV_STAGING__API_TOKEN": "st",
        "ENV_STAGING__SCHEDULE_OFFSET_SECONDS": "15",
        "ENV_STAGING__ENABLED": "true" if staging_enabled else "false",
    }
    with patch.dict(os.environ, env, clear=True):
        return load_settings(dotenv=False)


def test_build_jobs_one_per_enabled_env(tmp_path: Path) -> None:
    jobs = build_jobs(_settings(tmp_path))
    by_env = {j.env_name: j for j in jobs}
    assert set(by_env) == {"production", "staging"}
    assert by_env["production"].trigger_kwargs == {"second": 0, "minute": "*"}
    assert by_env["staging"].trigger_kwargs == {"second": 15, "minute": "*"}
    for j in jobs:
        assert isinstance(j, JobSpec)
        assert j.max_instances == 1
        assert j.coalesce is True
        assert j.misfire_grace_time == 30
        assert j.job_id == f"macmini:{j.env_name}"


def test_build_jobs_excludes_disabled_env(tmp_path: Path) -> None:
    jobs = build_jobs(_settings(tmp_path, staging_enabled=False))
    assert [j.env_name for j in jobs] == ["production"]


def test_scheduler_registers_jobs_with_apscheduler(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = Scheduler(s, run_env=lambda _name: None)
    sched.register()
    try:
        ids = {j.id for j in sched._scheduler.get_jobs()}
        assert ids == {"macmini:production", "macmini:staging"}
        for job in sched._scheduler.get_jobs():
            assert job.max_instances == 1
            assert job.coalesce is True
    finally:
        sched.stop()


def test_concurrent_jobs_use_distinct_threads(tmp_path: Path) -> None:
    """Two envs firing within the same second run on distinct threads (T033)."""
    s = _settings(tmp_path)
    seen: dict[str, int] = {}
    barrier = threading.Barrier(2, timeout=5)

    def run_env(name: str) -> None:
        barrier.wait()  # both must be in-flight simultaneously
        seen[name] = threading.get_ident()

    sched = Scheduler(s, run_env=run_env)
    # Fire both envs ~now, one second apart at most.
    sched.register(immediate=True)
    sched.start()
    try:
        deadline = time.monotonic() + 5
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        sched.stop()

    assert set(seen) == {"production", "staging"}
    assert seen["production"] != seen["staging"]  # distinct worker threads


def test_scheduled_run_wires_emit_to_sse_sink(tmp_path: Path) -> None:
    """Regression: a SCHEDULED run must build BatchRunner with the SSE
    emit callback wired, exactly like the dashboard's manual /run path.

    The bug: ``Scheduler._default_run_env`` constructed ``BatchRunner``
    with no ``emit=``, so scheduled runs updated state silently and
    pushed zero SSE events — an open dashboard never refreshed and stayed
    frozen at "idle — no run yet". This pins ``emit`` to
    ``app_state.emit_event`` so the wiring can't silently regress.
    """
    s = _settings(tmp_path)
    state = BatchRunState(s.machine, [e.name for e in s.environments])
    # No run_env override -> the default (production) dispatch path runs.
    sched = Scheduler(s, state=state)

    with patch("batch.BatchRunner") as mock_runner:
        sched._dispatch("production")

    mock_runner.assert_called_once()
    emit = mock_runner.call_args.kwargs.get("emit")
    assert emit is not None, "scheduled run built BatchRunner without emit="
    # Bound-method equality compares __self__/__func__, so this asserts the
    # callback is app_state.emit_event (the dashboard's SSE sink).
    assert emit == app_state.emit_event
    mock_runner.return_value.run_once.assert_called_once_with()
