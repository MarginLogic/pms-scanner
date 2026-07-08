"""
Env + machine-aware batch runner (004).

One :class:`BatchRunner` per (environment, machine). Per run:
crash-recover only this machine's stranded files → settle filter →
atomic-rename claim into ``in-progress/<machine>/`` → render pages →
upload to this env's backend → success → ``processed/`` else back to
the watch dir. All counters/errors flow into the per-env
:class:`BatchRunState`.
"""

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from .config import Environment
from .machine import MachineIdentity
from .notify import Notifier
from .pdf_processor import downsample_image, process_pdf
from .state import BatchRunState, ErrorRecord
from .uploader import UploadOutcome, upload_page

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".pdf", ".tif", ".tiff"}

# When the backend permanently rejects a page (e.g. HTTP 422 because the page
# is too large), re-upload ONLY that page at progressively lower resolution
# before giving up on the file. Ordered high→low.
DOWNSAMPLE_DPI_LADDER = (150, 75)

EventEmitter = Callable[[dict[str, object]], None]


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
        notifier: Notifier | None = None,
    ) -> None:
        self.env = env
        self.machine = machine
        self.state = state
        self._settle = settle_seconds
        self._timeout = upload_timeout_seconds
        self._max_retries = upload_max_retries
        self._retry_max_wait = upload_retry_max_wait_seconds
        self._emit = emit
        self._notifier = notifier
        self._tag = f"[env={env.name} machine={machine.name}]"
        self._ensure_dirs()

    # -- directories -----------------------------------------------------

    def _ensure_dirs(self) -> None:
        in_self = self.env.in_progress_dir(self.machine)
        self.env.in_progress_root.mkdir(parents=True, exist_ok=True)
        in_self.mkdir(parents=True, exist_ok=True)
        self.env.processed_dir.mkdir(parents=True, exist_ok=True)
        self.env.failed_dir.mkdir(parents=True, exist_ok=True)
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
        status = "failed"
        try:
            pages = process_pdf(claimed)
            total = len(pages)
            self.state.set_current(self.env.name, total_pages=total)
            all_ok = True
            unrecoverable = False
            for page_num, image, _uncertain, rotation in pages:
                outcome, dpi_used = self._upload_page_with_downsample(
                    claimed, page_num, total, image
                )
                if outcome is UploadOutcome.ACCEPTED:
                    self.state.add_pages_uploaded(self.env.name, 1)
                else:
                    all_ok = False
                    if outcome is UploadOutcome.REJECTED:
                        unrecoverable = True
                    self.state.add_error(
                        self.env.name,
                        ErrorRecord(
                            filename=name,
                            message=f"upload {outcome.value}",
                            page_num=page_num,
                        ),
                    )
                self.state.set_current(self.env.name, current_page=page_num)
                self._fire(
                    "page_done",
                    filename=name,
                    page_num=page_num,
                    total_pages=total,
                    success=outcome is UploadOutcome.ACCEPTED,
                    rotation_applied=rotation,
                    downsampled_to_dpi=dpi_used,
                )

            status = self._dispose(claimed, name, all_ok, unrecoverable)
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

    def _upload_page_with_downsample(
        self, claimed: Path, page_num: int, total: int, image: Image.Image
    ) -> tuple[UploadOutcome, int | None]:
        """Upload one page, shrinking it if the backend refuses it as too large.

        Returns ``(outcome, dpi_used)`` where ``dpi_used`` is the reduced DPI
        that got the page accepted (``None`` if the full-resolution page was
        accepted or no downsample succeeded). A permanent ``REJECTED`` triggers
        a retry of ONLY this page down the :data:`DOWNSAMPLE_DPI_LADDER`; a
        transient ``FAILED`` is returned as-is so the whole file is retried
        later rather than needlessly degraded.
        """
        outcome = self._upload(claimed, page_num, total, image)
        if outcome is not UploadOutcome.REJECTED:
            return outcome, None

        for dpi in DOWNSAMPLE_DPI_LADDER:
            smaller = downsample_image(image, dpi)
            retry = self._upload(claimed, page_num, total, smaller)
            if retry is UploadOutcome.ACCEPTED:
                logger.warning(
                    "%s page %d/%d of %s accepted after downsampling to %d DPI",
                    self._tag, page_num, total, claimed.name, dpi,
                )
                return retry, dpi
            if retry is UploadOutcome.FAILED:
                # Transient error mid-ladder — stop degrading; retry file later.
                return retry, None

        logger.error(
            "%s page %d/%d of %s still rejected at %d DPI — unrecoverable",
            self._tag, page_num, total, claimed.name, DOWNSAMPLE_DPI_LADDER[-1],
        )
        return UploadOutcome.REJECTED, None

    def _upload(
        self, claimed: Path, page_num: int, total: int, image: Image.Image
    ) -> UploadOutcome:
        return upload_page(
            self.env,
            claimed,
            page_num,
            total,
            image,
            timeout_seconds=self._timeout,
            max_retries=self._max_retries,
            retry_max_wait_seconds=self._retry_max_wait,
        )

    def _dispose(
        self, claimed: Path, name: str, all_ok: bool, unrecoverable: bool
    ) -> str:
        """Move the finished file to its destination and return a status label."""
        if all_ok:
            os.rename(claimed, self.env.processed_dir / name)
            self.state.add_files_processed(self.env.name, 1)
            logger.info("%s completed %s → processed/", self._tag, name)
            return "completed"
        if unrecoverable:
            # The backend permanently refused a page even downsampled. Quarantine
            # to failed/ so it is NOT re-scanned — this is what previously caused
            # the endless claim→reject→return-to-watch loop.
            os.rename(claimed, self.env.failed_dir / name)
            logger.error(
                "%s quarantined %s → failed/ (unrecoverable upload rejection)",
                self._tag,
                name,
            )
            if self._notifier is not None:
                self._notifier.notify_quarantine(
                    filename=name,
                    folder=str(self.env.failed_dir),
                    env=self.env.name,
                    machine=self.machine.name,
                    reason="backend permanently rejected a page even after "
                    "downsampling to the lowest fallback resolution",
                )
            return "failed_permanent"
        # Only transient failures: return to the watch dir for a later retry.
        os.rename(claimed, self.env.watch_dir / name)
        logger.warning(
            "%s returned %s to watch dir after transient upload failure(s)",
            self._tag,
            name,
        )
        return "failed"

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
