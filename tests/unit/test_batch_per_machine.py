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


# ---------------------------------------------------------------------------
# T038 — claim writes ONLY to in-progress/<self>/ (FR-007/018)
# ---------------------------------------------------------------------------


import os  # noqa: E402
import sys  # noqa: E402


def test_claim_writes_only_to_self_subfolder(tmp_path: Path) -> None:
    env = _env(tmp_path)
    macmini = MachineIdentity("macmini")
    nuc = MachineIdentity("nuc")
    state = BatchRunState(macmini, [env.name])
    runner = BatchRunner(env, macmini, state)

    # A peer subfolder exists with a peer file — must stay untouched.
    nuc_dir = env.in_progress_dir(nuc)
    nuc_dir.mkdir(parents=True, exist_ok=True)
    peer_file = nuc_dir / "peer.pdf"
    peer_file.write_bytes(b"peer")
    peer_before = peer_file.read_bytes()

    src = env.watch_dir / "mine.pdf"
    src.write_bytes(b"%PDF-1.4")
    dest = runner.claim_file(src)

    assert dest == env.in_progress_dir(macmini) / "mine.pdf"
    assert not (env.in_progress_root / "mine.pdf").exists()  # not bare root
    assert not (nuc_dir / "mine.pdf").exists()  # not peer subfolder
    assert peer_file.read_bytes() == peer_before  # peer untouched


@pytest.mark.skipif(
    sys.platform == "darwin", reason="SMB ACLs operator-managed on macOS"
)
def test_self_subfolder_created_mode_0700(tmp_path: Path) -> None:
    env = _env(tmp_path)
    machine = MachineIdentity("macmini")
    BatchRunner(env, machine, BatchRunState(machine, [env.name]))
    mode = os.stat(env.in_progress_dir(machine)).st_mode & 0o777
    assert mode == 0o700


# ---------------------------------------------------------------------------
# T040 — crash recovery touches ONLY the running machine's subfolder
# (FR-008, SC-011)
# ---------------------------------------------------------------------------


def test_recover_stranded_only_own_subfolder(tmp_path: Path) -> None:
    env = _env(tmp_path)
    macmini = MachineIdentity("macmini")
    nuc = MachineIdentity("nuc")
    state = BatchRunState(macmini, [env.name])
    runner = BatchRunner(env, macmini, state)

    mac_dir = env.in_progress_dir(macmini)
    nuc_dir = env.in_progress_dir(nuc)
    nuc_dir.mkdir(parents=True, exist_ok=True)
    (mac_dir / "a.pdf").write_bytes(b"a")
    (mac_dir / "b.pdf").write_bytes(b"b")
    (nuc_dir / "n1.pdf").write_bytes(b"n1")
    (nuc_dir / "n2.pdf").write_bytes(b"n2")
    nuc_snapshot = {p.name: p.read_bytes() for p in nuc_dir.iterdir()}

    recovered = runner.recover_stranded()

    assert sorted(recovered) == ["a.pdf", "b.pdf"]
    assert (env.watch_dir / "a.pdf").is_file()
    assert (env.watch_dir / "b.pdf").is_file()
    assert list(mac_dir.iterdir()) == []  # own subfolder drained
    # Peer subfolder byte-for-byte unchanged (SC-011).
    assert {p.name: p.read_bytes() for p in nuc_dir.iterdir()} == nuc_snapshot


def test_recover_stranded_name_conflict_gets_suffix(tmp_path: Path) -> None:
    env = _env(tmp_path)
    machine = MachineIdentity("macmini")
    runner = BatchRunner(env, machine, BatchRunState(machine, [env.name]))

    # Operator re-created a file with the same name while we were down.
    (env.watch_dir / "dup.pdf").write_bytes(b"new")
    (env.in_progress_dir(machine) / "dup.pdf").write_bytes(b"stranded")

    runner.recover_stranded()

    assert (env.watch_dir / "dup.pdf").read_bytes() == b"new"  # not clobbered
    recovered = [
        p
        for p in env.watch_dir.iterdir()
        if p.name.startswith("dup.pdf.recovered-")
    ]
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"stranded"


# --- coverage: recovery edge, run_once guard, failure/exception disposition ---

from unittest.mock import patch as _patch  # noqa: E402

import fitz  # noqa: E402


def _pdf(path: Path, pages: int = 1) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=612, height=792)
    doc.save(str(path))
    doc.close()


def _ok_post(*a, **k):
    from unittest.mock import MagicMock

    r = MagicMock()
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.json.return_value = {"batch_id": "b", "images": [{"original_file_name": "f"}],
                           "rejected": []}
    return r


def test_recover_stranded_missing_subfolder_returns_empty(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    import shutil

    shutil.rmtree(r.env.in_progress_dir(r.machine))
    assert r.recover_stranded() == []


def test_recover_stranded_skips_non_supported_entries(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    d = r.env.in_progress_dir(r.machine)
    (d / "note.txt").write_text("nope")
    (d / "sub").mkdir()
    assert r.recover_stranded() == []
    assert (d / "note.txt").exists()  # untouched


def test_run_once_missing_watch_dir_logs_and_returns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import shutil

    r = _runner(tmp_path)
    shutil.rmtree(r.env.watch_dir)
    with caplog.at_level(logging.ERROR, logger="scanner.batch"):
        r.run_once()
    assert any("missing" in rec.getMessage() for rec in caplog.records)


def _runner0(tmp_path: Path) -> BatchRunner:
    env = _env(tmp_path)
    m = MachineIdentity("macmini")
    return BatchRunner(env, m, BatchRunState(m, [env.name]), settle_seconds=0)


def test_run_once_upload_failure_returns_file_to_watch(tmp_path: Path) -> None:
    r = _runner0(tmp_path)
    _pdf(r.env.watch_dir / "f.pdf", pages=1)

    def fail_post(*a, **k):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 500
        from requests import HTTPError

        resp.raise_for_status.side_effect = HTTPError(response=resp)
        return resp

    with _patch("uploader.requests.post", side_effect=fail_post):
        with _patch("uploader.time.sleep"):
            r.run_once()

    assert (r.env.watch_dir / "f.pdf").is_file()  # returned for retry
    assert not (r.env.processed_dir / "f.pdf").exists()
    assert r.state.env("production").errors


def test_run_once_processing_exception_marks_failed(tmp_path: Path) -> None:
    r = _runner0(tmp_path)
    _pdf(r.env.watch_dir / "g.pdf", pages=1)
    with _patch("batch.process_pdf", side_effect=RuntimeError("render boom")):
        r.run_once()
    assert (r.env.watch_dir / "g.pdf").is_file()
    assert r.state.env("production").errors[-1].message


# ---------------------------------------------------------------------------
# Oversized-page downsample fallback + unrecoverable quarantine
# ---------------------------------------------------------------------------

from uploader import UploadOutcome  # noqa: E402


def test_rejected_page_recovered_by_downsampling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A page the backend refuses full-size is retried smaller; the file still
    completes to processed/ and is NOT quarantined or looped."""
    import logging

    r = _runner0(tmp_path)
    _pdf(r.env.watch_dir / "big.pdf", pages=1)

    # Full-resolution upload rejected (e.g. HTTP 422 too large); the first
    # downsample step (150 DPI) is accepted.
    outcomes = iter([UploadOutcome.REJECTED, UploadOutcome.ACCEPTED])
    with caplog.at_level(logging.WARNING, logger="scanner.batch"):
        with _patch("batch.upload_page", side_effect=lambda *a, **k: next(outcomes)):
            r.run_once()

    assert (r.env.processed_dir / "big.pdf").is_file()  # completed
    assert not (r.env.watch_dir / "big.pdf").exists()  # not looped
    assert not (r.env.failed_dir / "big.pdf").exists()  # not quarantined
    assert r.state.env("production").files_processed == 1
    assert r.state.env("production").pages_uploaded == 1
    assert any("downsampl" in rec.getMessage().lower() for rec in caplog.records)


def test_unrecoverable_rejection_quarantined_not_looped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A page rejected even at the lowest DPI moves the file to failed/ so it is
    never re-scanned — the fix for the claim→reject→return loop."""
    import logging
    from unittest.mock import MagicMock

    env = _env(tmp_path)
    m = MachineIdentity("macmini")
    notifier = MagicMock()
    r = BatchRunner(
        env, m, BatchRunState(m, [env.name]), settle_seconds=0, notifier=notifier
    )
    _pdf(env.watch_dir / "bad.pdf", pages=1)

    with caplog.at_level(logging.ERROR, logger="scanner.batch"):
        with _patch("batch.upload_page", return_value=UploadOutcome.REJECTED):
            r.run_once()

    assert (env.failed_dir / "bad.pdf").is_file()  # quarantined
    assert not (env.watch_dir / "bad.pdf").exists()  # NOT returned to watch
    assert not (env.processed_dir / "bad.pdf").exists()
    assert r.state.env("production").files_processed == 0
    assert r.state.env("production").errors
    assert any("quarantined" in rec.getMessage().lower() for rec in caplog.records)
    # Support is notified with the file name + quarantine folder.
    notifier.notify_quarantine.assert_called_once()
    kw = notifier.notify_quarantine.call_args.kwargs
    assert kw["filename"] == "bad.pdf"
    assert kw["folder"] == str(env.failed_dir)
    assert kw["env"] == "production" and kw["machine"] == "macmini"


def test_transient_failure_still_returns_to_watch(tmp_path: Path) -> None:
    """Transient (server/network) failures are NOT quarantined — the file goes
    back to the watch dir for a legitimate later retry."""
    r = _runner0(tmp_path)
    _pdf(r.env.watch_dir / "t.pdf", pages=1)

    with _patch("batch.upload_page", return_value=UploadOutcome.FAILED):
        r.run_once()

    assert (r.env.watch_dir / "t.pdf").is_file()  # retryable
    assert not (r.env.failed_dir / "t.pdf").exists()  # not quarantined
    assert not (r.env.processed_dir / "t.pdf").exists()
