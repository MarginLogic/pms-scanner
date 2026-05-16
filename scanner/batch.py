"""
Batch runner: scan watch folder, process each PDF/TIFF, upload every page.

Execution steps per run
-----------------------
1. Crash recovery — return any in-progress/ files to watch_dir.
2. Guard — if watch_dir does not exist, log ERROR and return.
3. Settle filter — skip files modified within file_settle_seconds.
4. Atomic claim — rename each file to in-progress/ (skip on lost-race).
5. Process — call pdf_processor.process_pdf() for each claimed file.
6. Upload — call uploader.upload_page() for each page.
7. Disposition — success → processed/; any failure → back to watch_dir.
8. State updates — update AppState under lock throughout.

Note: A fresh Settings() is created at the start of each function so that
environment variable patches in tests are correctly picked up.  The overhead
is negligible (< 1 ms) compared to file I/O and HTTP calls.
"""

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import Environment, Settings
from .machine import MachineIdentity
from .pdf_processor import process_pdf
from .state import AppState, BatchRunState, ErrorRecord, FileResult, PageResult, RunRecord
from .uploader import _legacy_upload_page as upload_page
from .uploader import upload_page as upload_page_env

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".pdf", ".tif", ".tiff"}

EventEmitter = Callable[[dict[str, object]], None]


def startup(state: AppState) -> None:
    """
    Ensure required directories exist and perform crash recovery.

    Call once before the scheduler starts.
    """
    cfg = Settings()
    watch_dir = Path(cfg.watch_dir)
    cfg.inprogress_dir.mkdir(parents=True, exist_ok=True)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Startup: watch=%s  in-progress=%s  processed=%s",
        watch_dir,
        cfg.inprogress_dir,
        cfg.processed_dir,
    )
    _recover_inprogress(cfg)


def execute_run(state: AppState) -> None:
    """
    Execute one full batch run.

    Thread-safe: multiple concurrent runs are allowed; each run gets its own
    RunRecord and claims files atomically so no file is processed twice.
    """
    cfg = Settings()

    run = RunRecord()
    with state._lock:
        state.current_run = run
        state.active_runs[run.run_id] = run
    state.emit_event({"type": "run_started", "run_id": run.run_id})

    logger.info("Batch run %s started", run.run_id)

    # Guard
    watch_dir = Path(cfg.watch_dir)
    if not watch_dir.exists():
        logger.error(
            "Watch directory %s does not exist — aborting run %s",
            watch_dir,
            run.run_id,
        )
        _finish_run(state, run, status="failed")
        return

    # Process each PDF
    pdfs = _find_settled_pdfs(watch_dir, cfg)
    logger.info("Run %s: found %d settled PDF(s)", run.run_id, len(pdfs))

    for pdf_path in pdfs:
        _process_one_file(pdf_path, run, state, cfg)

    _finish_run(state, run, status="completed")
    logger.info("Batch run %s completed (%d file(s))", run.run_id, len(run.files))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _recover_inprogress(cfg: Settings) -> list[str]:
    """Move all files from in-progress/ back to watch_dir. Return filenames."""
    recovered: list[str] = []
    inprogress = cfg.inprogress_dir
    if not inprogress.exists():
        return recovered
    watch_dir = Path(cfg.watch_dir)
    for stranded in inprogress.iterdir():
        if not stranded.is_file() or stranded.suffix.lower() not in SUPPORTED_EXTS:
            continue
        dest = watch_dir / stranded.name
        try:
            stranded.rename(dest)
            recovered.append(stranded.name)
        except OSError as exc:
            logger.warning("Could not recover %s: %s", stranded.name, exc)
    return recovered


def _find_settled_pdfs(watch_dir: Path, cfg: Settings) -> list[Path]:
    """Return PDF/TIFF files in watch_dir that have been stable for file_settle_seconds."""
    settle = cfg.file_settle_seconds
    now = time.time()
    settled = []
    for entry in watch_dir.iterdir():
        if not entry.is_file() or entry.suffix.lower() not in SUPPORTED_EXTS:
            continue
        age = now - entry.stat().st_mtime
        if age >= settle:
            settled.append(entry)
        else:
            logger.debug(
                "Skipping %s — only %.1fs old (settle=%.1fs)", entry.name, age, settle
            )
    return settled


def _process_one_file(
    pdf_path: Path,
    run: RunRecord,
    state: AppState,
    cfg: Settings,
) -> None:
    """Claim, process and upload one PDF file; update run state throughout."""
    inprogress_path = cfg.inprogress_dir / pdf_path.name

    # Step 4: atomic claim
    try:
        pdf_path.rename(inprogress_path)
    except FileNotFoundError:
        logger.debug("Lost race claiming %s — already taken", pdf_path.name)
        return
    logger.info("Claimed %s", pdf_path.name)

    file_result = FileResult(
        filename=pdf_path.name,
        total_pages=0,
        status="in_progress",
    )
    with state._lock:
        run.files.append(file_result)

    state.emit_event(
        {
            "type": "file_started",
            "run_id": run.run_id,
            "filename": pdf_path.name,
        }
    )

    try:
        # Step 5: process pages
        pages = process_pdf(inprogress_path)
        total_pages = len(pages)
        file_result.total_pages = total_pages

        all_success = True
        for page_num, pil_image, orientation_uncertain, rotation_applied in pages:
            success = upload_page(inprogress_path, page_num, total_pages, pil_image)
            if not success:
                logger.error(
                    "Upload failed: %s page %d/%d",
                    pdf_path.name,
                    page_num,
                    total_pages,
                )
                all_success = False

            page_result = PageResult(
                page_num=page_num,
                total_pages=total_pages,
                rotation_applied=rotation_applied,
                orientation_uncertain=orientation_uncertain,
                upload_success=success,
                error=None if success else "upload failed",
            )
            with state._lock:
                file_result.pages.append(page_result)

            state.emit_event(
                {
                    "type": "page_done",
                    "run_id": run.run_id,
                    "filename": pdf_path.name,
                    "page_num": page_num,
                    "total_pages": total_pages,
                    "upload_success": success,
                }
            )

        # Step 7: disposition
        if all_success:
            dest = cfg.processed_dir / pdf_path.name
            inprogress_path.rename(dest)
            file_result.status = "completed"
            logger.info("Completed %s → processed/", pdf_path.name)
        else:
            dest = Path(cfg.watch_dir) / pdf_path.name
            inprogress_path.rename(dest)
            file_result.status = "failed"
            logger.warning(
                "Returned %s to watch dir after upload failure(s)", pdf_path.name
            )

    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error processing %s: %s", pdf_path.name, exc)
        try:
            inprogress_path.rename(Path(cfg.watch_dir) / pdf_path.name)
        except OSError:
            pass
        file_result.status = "failed"

    finally:
        file_result.completed_at = datetime.now(UTC)
        state.emit_event(
            {
                "type": "file_done",
                "run_id": run.run_id,
                "filename": pdf_path.name,
                "status": file_result.status,
            }
        )


def _finish_run(state: AppState, run: RunRecord, status: str) -> None:
    run.status = status
    run.completed_at = datetime.now(UTC)
    with state._lock:
        state.last_run = run
        state.active_runs.pop(run.run_id, None)
        if state.current_run is run:
            state.current_run = next(iter(state.active_runs.values()), None)
        state.history.insert(0, run)
        del state.history[state.HISTORY_LIMIT:]
    state.emit_event(
        {
            "type": "run_done",
            "run_id": run.run_id,
            "status": status,
            "files": len(run.files),
        }
    )


# ===========================================================================
# 004 — env + machine-aware BatchRunner (US1/US4)
# ===========================================================================


class BatchRunner:
    """Processes one environment's watch folder for one machine.

    Files are claimed by atomic rename into ``env.in_progress_dir(machine)``
    (FR-007/017), processed page-by-page, uploaded to ``env``'s backend
    (FR-002/003/005), then moved to the shared ``processed/`` directory.
    All cross-environment state is isolated: a runner only ever touches its
    own env's tree and its own machine subfolder.
    """

    def __init__(
        self,
        env: Environment,
        machine: MachineIdentity,
        state: BatchRunState,
        *,
        settle_seconds: float = 10.0,
        upload_timeout_seconds: int = 30,
        upload_max_retries: int = 3,
        upload_retry_max_wait_seconds: int = 10,
        emit: EventEmitter | None = None,
    ) -> None:
        self.env = env
        self.machine = machine
        self.state = state
        self._settle = settle_seconds
        self._timeout = upload_timeout_seconds
        self._max_retries = upload_max_retries
        self._retry_max_wait = upload_retry_max_wait_seconds
        self._emit = emit
        self._tag = f"[env={env.name} machine={machine.name}]"
        self._ensure_dirs()

    # -- directories -----------------------------------------------------

    def _ensure_dirs(self) -> None:
        in_self = self.env.in_progress_dir(self.machine)
        self.env.in_progress_root.mkdir(parents=True, exist_ok=True)
        in_self.mkdir(parents=True, exist_ok=True)
        self.env.processed_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(in_self, 0o700)

    # -- claim (FR-017) --------------------------------------------------

    def claim_file(self, src: Path) -> Path | None:
        """Atomically claim ``src`` into this machine's in-progress subfolder.

        Returns the new path on success. Returns ``None`` (DEBUG log, no
        exception) if the source vanished — a peer won the claim race.
        """
        dest: Path = self.env.in_progress_dir(self.machine) / src.name
        try:
            os.rename(src, dest)
        except (FileNotFoundError, NotADirectoryError):
            logger.debug(
                "%s lost claim race for %s — already taken by a peer",
                self._tag,
                src.name,
            )
            return None
        logger.info("%s claimed %s", self._tag, src.name)
        return dest

    # -- crash recovery (FR-008; refined in T041) ------------------------

    def recover_stranded(self) -> list[str]:
        """Return this machine's own stranded in-progress files to watch_dir.

        Reads ONLY ``env.in_progress_dir(machine)`` — never a peer subfolder.
        """
        recovered: list[str] = []
        in_self = self.env.in_progress_dir(self.machine)
        try:
            entries = list(in_self.iterdir())
        except FileNotFoundError:
            return recovered
        for stranded in entries:
            if (
                not stranded.is_file()
                or stranded.suffix.lower() not in SUPPORTED_EXTS
            ):
                continue
            dest = self.env.watch_dir / stranded.name
            if dest.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                dest = self.env.watch_dir / f"{stranded.name}.recovered-{stamp}"
                logger.warning(
                    "%s recovery name conflict — restoring %s as %s",
                    self._tag,
                    stranded.name,
                    dest.name,
                )
            os.rename(stranded, dest)
            recovered.append(stranded.name)
        if recovered:
            logger.info(
                "%s recovered %d stranded file(s)", self._tag, len(recovered)
            )
        return recovered

    # -- one pass --------------------------------------------------------

    def run_once(self) -> None:
        watch = self.env.watch_dir
        if not watch.exists():
            logger.error("%s watch dir %s missing — skipping run", self._tag, watch)
            return

        self.state.mark_run_started(self.env.name, datetime.now(UTC))
        self._fire("run_started")

        for src in self._find_settled(watch):
            claimed = self.claim_file(src)
            if claimed is None:
                continue
            self._process_file(claimed)

        self.state.mark_run_finished(self.env.name, datetime.now(UTC))
        self._fire("run_done")

    def _find_settled(self, watch: Path) -> list[Path]:
        now = time.time()
        settled: list[Path] = []
        for entry in watch.iterdir():
            if (
                not entry.is_file()
                or entry.suffix.lower() not in SUPPORTED_EXTS
            ):
                continue
            if now - entry.stat().st_mtime >= self._settle:
                settled.append(entry)
        return settled

    def _process_file(self, claimed: Path) -> None:
        name = claimed.name
        self.state.set_current(self.env.name, current_file=name, current_page=0)
        self._fire("file_started", filename=name)
        try:
            pages = process_pdf(claimed)
            total = len(pages)
            self.state.set_current(self.env.name, total_pages=total)
            all_ok = True
            for page_num, image, _uncertain, rotation in pages:
                ok = upload_page_env(
                    self.env,
                    claimed,
                    page_num,
                    total,
                    image,
                    timeout_seconds=self._timeout,
                    max_retries=self._max_retries,
                    retry_max_wait_seconds=self._retry_max_wait,
                )
                if ok:
                    self.state.add_pages_uploaded(self.env.name, 1)
                else:
                    all_ok = False
                    self.state.add_error(
                        self.env.name,
                        ErrorRecord(
                            filename=name,
                            message="upload failed",
                            page_num=page_num,
                        ),
                    )
                self.state.set_current(self.env.name, current_page=page_num)
                self._fire(
                    "page_done",
                    filename=name,
                    page_num=page_num,
                    total_pages=total,
                    success=ok,
                    rotation_applied=rotation,
                )

            if all_ok:
                os.rename(claimed, self.env.processed_dir / name)
                self.state.add_files_processed(self.env.name, 1)
                status = "completed"
                logger.info("%s completed %s → processed/", self._tag, name)
            else:
                os.rename(claimed, self.env.watch_dir / name)
                status = "failed"
                logger.warning(
                    "%s returned %s to watch dir after upload failure(s)",
                    self._tag,
                    name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("%s error processing %s: %s", self._tag, name, exc)
            self.state.add_error(
                self.env.name,
                ErrorRecord(filename=name, message=str(exc)),
            )
            try:
                os.rename(claimed, self.env.watch_dir / name)
            except OSError:
                pass
            status = "failed"
        finally:
            self.state.set_current(self.env.name, current_file=None)
            self._fire("file_done", filename=name, status=status)

    # -- events ----------------------------------------------------------

    def _fire(self, event_type: str, **data: object) -> None:
        if self._emit is None:
            return
        self._emit(
            {
                "type": event_type,
                "env": self.env.name,
                "machine": self.machine.name,
                **data,
            }
        )
