"""Unit tests for the env+machine-aware BatchRunner — T021 (004 US1).

claim_file(src) must atomically move into ``env.in_progress_dir(machine)``
only — never the bare in-progress/ root, never a peer subfolder — and
must return None (DEBUG log, no exception) when the source vanished
because a peer won the claim race (FR-017).
"""
import logging
from pathlib import Path

import pytest
from batch import BatchRunner
from config import Environment
from machine import MachineIdentity
from pydantic import SecretStr
from state import BatchRunState


def _env(tmp_path: Path, name: str = "production") -> Environment:
    watch = tmp_path / name
    watch.mkdir(parents=True, exist_ok=True)
    return Environment(
        name=name,  # type: ignore[arg-type]
        watch_dir=watch,
        backend_base_url="https://adg.mpsinc.io",
        api_token=SecretStr("tok"),
        schedule_offset_seconds=0,
    )


def _runner(tmp_path: Path, machine_name: str = "macmini") -> BatchRunner:
    env = _env(tmp_path)
    machine = MachineIdentity(machine_name)
    state = BatchRunState(machine, [env.name])
    return BatchRunner(env, machine, state)


def test_claim_moves_into_env_in_progress_self_subfolder(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    src = r.env.watch_dir / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    dest = r.claim_file(src)

    expected = r.env.in_progress_dir(r.machine) / "scan.pdf"
    assert dest == expected
    assert dest.is_file()
    assert not src.exists()
    # Never the bare in-progress/ root, never a peer subfolder.
    assert not (r.env.in_progress_root / "scan.pdf").exists()


def test_claim_returns_none_when_source_vanished(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = _runner(tmp_path)
    missing = r.env.watch_dir / "ghost.pdf"  # never created — peer won

    with caplog.at_level(logging.DEBUG, logger="scanner.batch"):
        result = r.claim_file(missing)

    assert result is None  # no exception raised
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_claim_target_directory_exists_after_construction(
    tmp_path: Path,
) -> None:
    r = _runner(tmp_path)
    assert r.env.in_progress_dir(r.machine).is_dir()


def test_runner_carries_env_and_machine(tmp_path: Path) -> None:
    r = _runner(tmp_path, machine_name="nuc")
    assert r.env.name == "production"
    assert r.machine.name == "nuc"
    assert isinstance(r.state, BatchRunState)
