"""Unit tests for the __main__ startup orchestration — T018 (004).

Covers the data-model startup-validation order that __main__ performs
after config load: NTP gate (FR-022/024), per-machine directory
creation (data-model §6), BatchRunState assembly, and DriftMonitor
wiring (created, sink=state, not yet started).
"""
import logging
import os
from datetime import UTC, datetime

import pytest
from config import load_settings
from ntp import NTPMeasurement, NTPStartupError

import scanner.__main__ as main_mod


class _FakeClient:
    source = "pool.ntp.org"

    def __init__(self, offset: float = 0.0) -> None:
        self._offset = offset

    def measure(self) -> NTPMeasurement:
        return NTPMeasurement(
            "pool.ntp.org", self._offset, datetime.now(UTC), "ok", 2
        )


def _settings(tmp_path, **overrides):
    prod = tmp_path / "prod"
    stg = tmp_path / "stg"
    prod.mkdir()
    stg.mkdir()
    env = {
        "MACHINE_IDENTITY": "macmini",
        "NTP__STARTUP_REQUIRED": "true",
        "NTP__MAX_DRIFT_SECONDS": "1.0",
        "NTP__STARTUP_TIMEOUT_SECONDS": "5",
        "ENVIRONMENTS": "production,staging",
        "ENV_PRODUCTION__WATCH_DIR": str(prod),
        "ENV_PRODUCTION__API_TOKEN": "ptok",
        "ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS": "0",
        "ENV_STAGING__WATCH_DIR": str(stg),
        "ENV_STAGING__API_TOKEN": "stok",
        "ENV_STAGING__SCHEDULE_OFFSET_SECONDS": "15",
    }
    env.update(overrides)
    from unittest.mock import patch

    with patch.dict(os.environ, env, clear=True):
        return load_settings(dotenv=False)


def test_build_runtime_creates_per_machine_dirs(tmp_path) -> None:
    s = _settings(tmp_path)
    rt = main_mod.build_runtime(s, ntp_client=_FakeClient(0.0))
    for env in s.environments:
        assert env.in_progress_dir(s.machine).is_dir()
        assert env.processed_dir.is_dir()
    if os.name == "posix":
        mode = (
            s.environments[0].in_progress_dir(s.machine).stat().st_mode & 0o777
        )
        assert mode == 0o700


def test_build_runtime_assembles_state_and_unstarted_monitor(tmp_path) -> None:
    s = _settings(tmp_path)
    rt = main_mod.build_runtime(s, ntp_client=_FakeClient(0.0))
    assert set(rt.state.per_env) == {"production", "staging"}
    assert rt.state.machine == s.machine
    # DriftMonitor created, wired to state as its sink, not started.
    assert rt.drift_monitor is not None
    assert rt.drift_monitor._sink is rt.state
    assert rt.drift_monitor._thread is None


def test_build_runtime_records_startup_clock_sync(tmp_path) -> None:
    s = _settings(tmp_path)
    rt = main_mod.build_runtime(s, ntp_client=_FakeClient(0.043))
    assert rt.state.recent_clock_sync is not None
    assert rt.state.recent_clock_sync.outcome == "ok"


def test_build_runtime_refuses_to_start_on_excess_drift(tmp_path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(NTPStartupError):
        main_mod.build_runtime(s, ntp_client=_FakeClient(9.0))


def test_build_runtime_skips_gate_when_not_required(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    s = _settings(tmp_path, **{"NTP__STARTUP_REQUIRED": "false"})
    with caplog.at_level(logging.WARNING):
        rt = main_mod.build_runtime(s, ntp_client=_FakeClient(9.0))
    # Excess drift would have raised if the gate ran; it didn't.
    assert set(rt.state.per_env) == {"production", "staging"}
    assert any("ntp" in r.message.lower() for r in caplog.records)


def test_configure_services_registers_jobs_and_wires_dashboard(
    tmp_path,
) -> None:
    import dashboard

    s = _settings(tmp_path)
    rt = main_mod.build_runtime(s, ntp_client=_FakeClient(0.0))
    sched = main_mod.configure_services(rt)
    try:
        ids = {j.id for j in sched._scheduler.get_jobs()}
        assert ids == {"macmini:production", "macmini:staging"}
        # Dashboard wired to the same runtime state.
        assert dashboard._settings is s
        assert dashboard._run_state is rt.state
    finally:
        sched.stop()
        dashboard._settings = None
        dashboard._run_state = None


def test_configure_services_scheduler_supports_legacy_shutdown(
    tmp_path,
) -> None:
    # _shutdown() calls scheduler.shutdown(wait=True); the Scheduler
    # wrapper must accept that for legacy signal-handler compatibility.
    s = _settings(tmp_path)
    rt = main_mod.build_runtime(s, ntp_client=_FakeClient(0.0))
    sched = main_mod.configure_services(rt)
    sched.start()
    sched.shutdown(wait=True)
    assert sched.running is False
