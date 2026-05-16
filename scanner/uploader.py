"""
Upload a rendered page to ``{env.backend_base_url}/api/scanned-images/upload``.

The destination host and credentials are carried by the :class:`Environment`
passed in explicitly (FR-002/003/005) — there is no module-level config and no
hard-coded host, so production/staging routing is impossible to miswire.

Retry strategy
--------------
Transient server errors (HTTP 5xx and network failures) are retried up to
``max_retries`` total attempts with exponential back-off capped at
``retry_max_wait_seconds``. Client errors (HTTP 4xx) are NOT retried.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from pathlib import Path

import requests
from PIL import Image

from .config import Environment, settings

logger = logging.getLogger("scanner.uploader")

# Client-side rate limiter: at most 60 HTTP requests per 60-second window,
# including retries. Backend enforces the same quota and returns 429 otherwise.
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW = 60.0
_rate_lock = threading.Lock()
_rate_history: deque[float] = deque()


def _rate_limit_acquire() -> None:
    """Block until a request slot is available within the rolling window."""
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _rate_history and now - _rate_history[0] >= _RATE_LIMIT_WINDOW:
                _rate_history.popleft()
            if len(_rate_history) < _RATE_LIMIT_MAX:
                _rate_history.append(now)
                return
            sleep_for = _RATE_LIMIT_WINDOW - (now - _rate_history[0]) + 0.05
        logger.info("Rate limit hit — sleeping %.2fs", sleep_for)
        time.sleep(sleep_for)


def upload_page(
    env: Environment,
    path: Path,
    page_num: int,
    total_pages: int,
    image: Image.Image,
    *,
    timeout_seconds: int = 30,
    max_retries: int = 3,
    retry_max_wait_seconds: int = 10,
) -> bool:
    """Upload one rendered page to ``env``'s backend.

    Returns ``True`` if the backend accepted the image, ``False`` on any
    permanent or exhausted-retry failure. The destination and token come
    solely from ``env``.
    """
    filename = f"{path.stem}_p{page_num:03d}.tiff"
    url = f"{env.backend_base_url}/api/scanned-images/upload"
    headers = {"Authorization": f"Bearer {env.api_token.get_secret_value()}"}

    form_data: dict[str, str] = {}
    if env.requisition_id is not None:
        form_data["requisition_id"] = str(env.requisition_id)

    for attempt in range(max_retries):
        image_bytes = _encode_tiff(image)
        _rate_limit_acquire()
        try:
            response = requests.post(
                url,
                headers=headers,
                files=[("files", (filename, image_bytes, "image/tiff"))],
                data=form_data,
                timeout=timeout_seconds,
            )
            response.raise_for_status()

            body = response.json()
            batch_id = body.get("batch_id", "?")
            accepted = [img["original_file_name"] for img in body.get("images", [])]
            rejected = body.get("rejected", [])

            if accepted:
                logger.info(
                    "[env=%s] Uploaded %s (page %d/%d) → batch %s",
                    env.name,
                    filename,
                    page_num,
                    total_pages,
                    batch_id,
                )
            for rej in rejected:
                logger.warning(
                    "[env=%s] Backend rejected %s in batch %s: %s",
                    env.name,
                    rej.get("file_name"),
                    batch_id,
                    rej.get("reason"),
                )
            return bool(accepted)

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500:
                logger.error(
                    "[env=%s] HTTP %d uploading %s (page %d/%d): %s",
                    env.name,
                    status,
                    filename,
                    page_num,
                    total_pages,
                    exc,
                )
                return False
            logger.warning(
                "[env=%s] HTTP %d uploading %s (page %d/%d), attempt %d/%d",
                env.name,
                status,
                filename,
                page_num,
                total_pages,
                attempt + 1,
                max_retries,
            )

        except requests.RequestException as exc:
            logger.warning(
                "[env=%s] Network error uploading %s (page %d/%d), "
                "attempt %d/%d: %s",
                env.name,
                filename,
                page_num,
                total_pages,
                attempt + 1,
                max_retries,
                exc,
            )

        if attempt < max_retries - 1:
            wait_seconds = min(2**attempt, retry_max_wait_seconds)
            time.sleep(wait_seconds)

    logger.error(
        "[env=%s] Exhausted %d upload attempts for %s (page %d/%d)",
        env.name,
        max_retries,
        filename,
        page_num,
        total_pages,
    )
    return False


def _encode_tiff(image: Image.Image) -> bytes:
    """Encode a PIL Image to TIFF bytes (LZW-compressed, lossless)."""
    buf = io.BytesIO()
    image.save(buf, format="TIFF", compression="tiff_lzw")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 003-era single-env shim — used by the legacy batch path until T022.
# Removed alongside the legacy Settings (tasks.md T057).
# ---------------------------------------------------------------------------


def _legacy_upload_page(
    path: Path,
    page_num: int,
    total_pages: int,
    image: Image.Image,
) -> bool:
    filename = f"{path.stem}_p{page_num:03d}.tiff"
    url = f"{settings.backend_base_url}/api/scanned-images/upload"
    headers = {"X-API-Key": settings.api_token}
    form_data: dict[str, str] = {}
    if settings.requisition_id:
        form_data["requisition_id"] = str(settings.requisition_id)

    max_attempts = settings.upload_max_retries
    max_wait = settings.upload_retry_max_wait_seconds

    for attempt in range(max_attempts):
        image_bytes = _encode_tiff(image)
        _rate_limit_acquire()
        try:
            response = requests.post(
                url,
                headers=headers,
                files=[("files", (filename, image_bytes, "image/tiff"))],
                data=form_data,
                timeout=settings.upload_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            batch_id = body.get("batch_id", "?")
            accepted = [img["original_file_name"] for img in body.get("images", [])]
            rejected = body.get("rejected", [])
            if accepted:
                logger.info(
                    "Uploaded %s (page %d/%d) → batch %s",
                    filename,
                    page_num,
                    total_pages,
                    batch_id,
                )
            for rej in rejected:
                logger.warning(
                    "Backend rejected %s in batch %s: %s",
                    rej.get("file_name"),
                    batch_id,
                    rej.get("reason"),
                )
            return bool(accepted)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500:
                logger.error(
                    "HTTP %d uploading %s (page %d/%d): %s",
                    status,
                    filename,
                    page_num,
                    total_pages,
                    exc,
                )
                return False
            logger.warning(
                "HTTP %d uploading %s (page %d/%d), attempt %d/%d",
                status,
                filename,
                page_num,
                total_pages,
                attempt + 1,
                max_attempts,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Network error uploading %s (page %d/%d), attempt %d/%d: %s",
                filename,
                page_num,
                total_pages,
                attempt + 1,
                max_attempts,
                exc,
            )
        if attempt < max_attempts - 1:
            wait_seconds = min(2**attempt, max_wait)
            time.sleep(wait_seconds)

    logger.error(
        "Exhausted %d upload attempts for %s (page %d/%d)",
        max_attempts,
        filename,
        page_num,
        total_pages,
    )
    return False
