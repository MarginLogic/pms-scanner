"""Unit tests for scanner/state.py — T015/T017 (004)."""
import logging
import threading
from datetime import UTC, datetime

import pytest
from machine import MachineIdentity
from ntp import ClockSyncEvent
from state import (
    BatchRunState,
    ErrorRecord,
    PerEnvRunState,
    make_logger,
)

# ---------------------------------------------------------------------------
# T015 — per-(machine, env) BatchRunState container
# ---------------------------------------------------------------------------


def _state() -> BatchRunState:
    return BatchRunState(MachineIdentity("macmini"), ["production", "staging"])


def test_batch_run_state_keyed_by_env_name() -> None:
    s = _state()
    assert set(s.per_env) == {"production", "staging"}
    prod = s.env("production")
    assert isinstance(prod, PerEnvRunState)
    assert prod.environment == "production"
    assert prod.machine == "macmini"


def test_unknown_env_raises_key_error() -> None:
    with pytest.raises(KeyError):
        _state().env("qa")


def test_per_env_counters_move_independently() -> None:
    s = _state()
    s.add_pages_uploaded("production", 3)
    s.add_files_processed("production", 1)
    assert s.env("production").pages_uploaded == 3
    assert s.env("production").files_processed == 1
    assert s.env("staging").pages_uploaded == 0
    assert s.env("staging").files_processed == 0


def test_add_error_is_scoped_per_env() -> None:
    s = _state()
    s.add_error(
        "staging",
        ErrorRecord(filename="x.pdf", message="boom", page_num=2),
    )
    assert len(s.env("staging").errors) == 1
    assert s.env("staging").errors[0].filename == "x.pdf"
    assert s.env("production").errors == []


def test_reset_daily_zeroes_counters_for_all_envs() -> None:
    s = _state()
    s.add_pages_uploaded("production", 7)
    s.add_files_processed("production", 2)
    s.add_pages_uploaded("staging", 3)
    s.add_files_processed("staging", 1)
    s.add_error(
        "production", ErrorRecord(filename="x.pdf", message="boom")
    )

    s.reset_daily()

    for name in ("production", "staging"):
        st = s.env(name)
        assert st.files_processed == 0
        assert st.pages_uploaded == 0
        assert st.errors == []


def test_reset_daily_preserves_live_and_last_run_fields() -> None:
    s = _state()
    when = datetime.now(UTC)
    s.set_current("production", current_file="live.pdf", current_page=4)
    s.mark_run_started("production", when)
    s.mark_run_finished("production", when)

    s.reset_daily()

    st = s.env("production")
    assert st.current_file == "live.pdf"
    assert st.current_page == 4
    assert st.last_run_started_at == when
    assert st.last_run_finished_at == when


def test_clock_sync_event_storage() -> None:
    s = _state()
    ok = ClockSyncEvent(datetime.now(UTC), "pool.ntp.org", 0.01, "ok")
    s.record_clock_sync(ok)
    assert s.recent_clock_sync is ok
    assert s.last_drift_warning is None

    bad = ClockSyncEvent(
        datetime.now(UTC), "pool.ntp.org", 5.0, "drift_uncorrected", 1
    )
    s.record_clock_sync(bad)
    assert s.recent_clock_sync is bad
    assert s.last_drift_warning is bad


def test_concurrent_mutation_under_rlock_loses_nothing() -> None:
    s = _state()
    threads_n, per_thread = 16, 500

    def worker() -> None:
        for _ in range(per_thread):
            s.add_pages_uploaded("production", 1)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.env("production").pages_uploaded == threads_n * per_thread


def test_state_satisfies_drift_sink_protocol() -> None:
    # DriftMonitor sets these attributes directly; they must exist + be writable.
    s = _state()
    ev = ClockSyncEvent(datetime.now(UTC), "src", 0.0, "ok")
    s.recent_clock_sync = ev
    s.last_drift_warning = ev
    assert s.recent_clock_sync is ev


# ---------------------------------------------------------------------------
# T017 — env+machine logger adapter with secret redaction
# ---------------------------------------------------------------------------


def test_logger_tags_env_and_machine(caplog: pytest.LogCaptureFixture) -> None:
    log = make_logger("scanner.test", machine="macmini", env="production")
    with caplog.at_level(logging.INFO, logger="scanner.test"):
        log.info("hello")
    msg = caplog.records[-1].getMessage()
    assert "machine=macmini" in msg
    assert "env=production" in msg


def test_logger_redacts_registered_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = make_logger(
        "scanner.test2",
        machine="nuc",
        env="staging",
        secrets=["pms_supersecret_token"],
    )
    with caplog.at_level(logging.INFO, logger="scanner.test2"):
        log.info("auth header bearer pms_supersecret_token done")
    msg = caplog.records[-1].getMessage()
    assert "pms_supersecret_token" not in msg
    assert "***" in msg


def test_logger_redacts_secrets(
    caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    """Environment.api_token is SecretStr and never reaches a log line (T030)."""
    from config import Environment
    from pydantic import SecretStr

    token = "pms_prod_DO_NOT_LEAK_4f3a"
    env = Environment(
        name="production",
        watch_dir=tmp_path / "p",
        backend_base_url="https://adg.mpsinc.io",
        api_token=SecretStr(token),
        schedule_offset_seconds=0,
    )
    # SecretStr never renders its value in repr/str.
    assert isinstance(env.api_token, SecretStr)
    assert token not in repr(env)
    assert token not in str(env.api_token)

    log = make_logger(
        "scanner.t030",
        machine="macmini",
        env="production",
        secrets=[env.api_token.get_secret_value()],
    )
    with caplog.at_level(logging.DEBUG, logger="scanner.t030"):
        log.info("uploading with Authorization: Bearer %s", token)
        log.debug("retry; token=%s", token)
    for record in caplog.records:
        assert token not in record.getMessage()
