"""
Targeted tests to close coverage gaps identified by pytest-cov.

Covers:
- scanner/batch.py:  startup(), OSError in recover, FileNotFoundError in claim,
                     exception in process_one_file
- scanner/uploader.py: requisition_id path, rejected-items warning,
                        RequestException path
- scanner/pdf_processor.py: _parse_osd_string (string OSD output path)
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# batch.py coverage
# ---------------------------------------------------------------------------


def test_startup_creates_dirs(tmp_path: Path):
    """batch.startup() creates in-progress/ and processed/ directories."""
    watch_dir = tmp_path / "ARIAscans"
    watch_dir.mkdir()

    with patch.dict(
        os.environ,
        {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t", "WATCH_DIR": str(watch_dir)},
    ):
        from state import AppState

        state = AppState()
        from batch import startup

        startup(state)

    assert (watch_dir / "in-progress").exists()
    assert (watch_dir / "processed").exists()


def test_recover_inprogress_os_error_is_logged(tmp_path: Path, caplog):
    """OSError during recovery is logged as WARNING and does not crash."""
    watch_dir = tmp_path / "ARIAscans"
    watch_dir.mkdir()
    (watch_dir / "in-progress").mkdir()
    stranded = watch_dir / "in-progress" / "stuck.pdf"
    stranded.write_bytes(b"dummy")

    import logging

    with patch.dict(
        os.environ,
        {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t", "WATCH_DIR": str(watch_dir)},
    ):
        from state import AppState

        state = AppState()
        with caplog.at_level(logging.WARNING, logger="batch"):
            with patch("pathlib.Path.rename", side_effect=OSError("permission denied")):
                from batch import startup

                startup(state)

    assert any("could not recover" in r.message.lower() for r in caplog.records)


def test_file_not_found_on_claim_skips_silently(tmp_path: Path):
    """FileNotFoundError when claiming a file (lost-race) is silently skipped."""
    watch_dir = tmp_path / "ARIAscans"
    watch_dir.mkdir()
    (watch_dir / "in-progress").mkdir()
    (watch_dir / "processed").mkdir()

    pdf = watch_dir / "race.pdf"
    pdf.write_bytes(b"dummy")

    with patch.dict(
        os.environ,
        {
            "BACKEND_BASE_URL": "http://x",
            "API_TOKEN": "t",
            "WATCH_DIR": str(watch_dir),
            "FILE_SETTLE_SECONDS": "0",
        },
    ):
        from state import AppState

        state = AppState()
        original_rename = Path.rename

        def fail_rename(self, dest):
            if "in-progress" in str(dest):
                raise FileNotFoundError("already moved")
            return original_rename(self, dest)

        with patch.object(Path, "rename", fail_rename):
            from batch import execute_run

            execute_run(state)

    # Run should complete without error; file still in watch dir (not moved)
    assert pdf.exists()


def test_exception_in_process_returns_file_to_watch(tmp_path: Path):
    """If process_pdf raises, the file is returned from in-progress/ to watch_dir."""
    watch_dir = tmp_path / "ARIAscans"
    watch_dir.mkdir()
    (watch_dir / "in-progress").mkdir()
    (watch_dir / "processed").mkdir()

    pdf = watch_dir / "bad.pdf"
    pdf.write_bytes(b"corrupt")

    with patch.dict(
        os.environ,
        {
            "BACKEND_BASE_URL": "http://x",
            "API_TOKEN": "t",
            "WATCH_DIR": str(watch_dir),
            "FILE_SETTLE_SECONDS": "0",
        },
    ):
        from state import AppState

        state = AppState()
        with patch("batch.process_pdf", side_effect=RuntimeError("bad PDF")):
            from batch import execute_run

            execute_run(state)

    # File should be back in watch_dir
    assert (watch_dir / "bad.pdf").exists()
    assert not (watch_dir / "in-progress" / "bad.pdf").exists()


# ---------------------------------------------------------------------------
# uploader.py coverage
# ---------------------------------------------------------------------------


def test_upload_page_includes_requisition_id(tmp_path: Path):
    """When requisition_id is set on settings, it's included in form_data."""
    from uuid import UUID

    pdf_path = tmp_path / "scan.pdf"
    img = Image.new("RGB", (10, 10))
    req_id = UUID("00000000-0000-0000-0000-000000000001")

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        import uploader

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status.return_value = None
        ok_resp.json.return_value = {
            "batch_id": "b",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        }
        # Patch settings.requisition_id directly (avoids singleton caching issue)
        with patch.object(uploader.settings, "requisition_id", req_id):
            with patch("uploader.requests.post", return_value=ok_resp) as mock_post:
                uploader._legacy_upload_page(pdf_path, 1, 1, img)

    call_data = mock_post.call_args.kwargs.get("data", {})
    assert call_data.get("requisition_id") == str(req_id)


def test_upload_page_logs_rejected_items(tmp_path: Path, caplog):
    """Backend-rejected items are logged as WARNING."""
    import logging

    pdf_path = tmp_path / "scan.pdf"
    img = Image.new("RGB", (10, 10))

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        from uploader import _legacy_upload_page as upload_page

        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "batch_id": "b",
            "images": [],
            "rejected": [{"file_name": "scan_p001.jpg", "reason": "duplicate"}],
        }
        with patch("uploader.requests.post", return_value=resp):
            with caplog.at_level(logging.WARNING, logger="uploader"):
                result = upload_page(pdf_path, 1, 1, img)

    assert result is False  # no accepted images
    assert any("rejected" in r.message.lower() for r in caplog.records)


def test_upload_page_request_exception_retries(tmp_path: Path):
    """Network-level RequestException triggers retry logic and eventually returns False."""
    import requests as req_lib

    pdf_path = tmp_path / "scan.pdf"
    img = Image.new("RGB", (10, 10))

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        from uploader import _legacy_upload_page as upload_page

        with patch(
            "uploader.requests.post",
            side_effect=req_lib.ConnectionError("network error"),
        ):
            with patch("uploader.time.sleep"):
                result = upload_page(pdf_path, 1, 1, img)

    assert result is False


# ---------------------------------------------------------------------------
# pdf_processor.py coverage — _parse_osd_string branch
# ---------------------------------------------------------------------------


def test_pdf_processor_parses_osd_string_output(tmp_path: Path):
    """When pytesseract returns a string (not dict), _parse_osd_string extracts rotation."""
    pages = [MagicMock()]
    page = pages[0]
    page.rotation = 0
    pixmap = MagicMock()
    pixmap.width = 10
    pixmap.height = 10
    pixmap.samples = b"\x80" * 300
    page.get_pixmap.return_value = pixmap

    doc = MagicMock()
    doc.__len__.return_value = 1
    doc.__enter__.return_value = doc
    doc.__exit__.return_value = False
    doc.load_page.return_value = page

    # Tesseract OSD string output format
    osd_string = (
        "Page number: 0\n"
        "Orientation in degrees: 90\n"
        "Rotate: 90\n"
        "Orientation confidence: 4.50\n"
    )

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                with patch(
                    "pdf_processor.pytesseract.image_to_osd",
                    return_value=osd_string,
                ):
                    from pdf_processor import process_pdf

                    result = process_pdf(tmp_path / "test.pdf")

    _page_num, _img, uncertain, rotation = result[0]
    assert rotation == 90
    assert uncertain is False
