"""Unit tests for scanner/uploader.py — T007."""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image


@pytest.fixture()
def dummy_image() -> Image.Image:
    return Image.new("RGB", (100, 100), color=(128, 128, 128))


@pytest.fixture()
def pdf_path(tmp_path: Path) -> Path:
    return tmp_path / "scan_20260414.pdf"


def _make_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    if status >= 400:
        from requests import HTTPError

        resp.raise_for_status.side_effect = HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _ok_resp() -> MagicMock:
    return _make_response(
        200,
        {
            "batch_id": "abc",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        },
    )


def test_upload_page_success(pdf_path: Path, dummy_image: Image.Image):
    """Successful POST returns True."""
    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        from uploader import _legacy_upload_page as upload_page

        with patch("uploader.requests.post", return_value=_ok_resp()) as mock_post:
            result = upload_page(pdf_path, 1, 5, dummy_image)
    assert result is True
    assert mock_post.called


def test_upload_page_filename_convention(pdf_path: Path, dummy_image: Image.Image):
    """Per-page filename follows {stem}_p{num:03d}.tiff convention."""
    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        from uploader import _legacy_upload_page as upload_page

        with patch("uploader.requests.post", return_value=_ok_resp()) as mock_post:
            upload_page(pdf_path, 7, 33, dummy_image)
        call_kwargs = mock_post.call_args
        files_arg = (
            call_kwargs.kwargs.get("files")
            or call_kwargs[1].get("files")
            or call_kwargs[0][2]
        )
        sent_filename = files_arg[0][1][0]
    assert sent_filename == "scan_20260414_p007.tiff"


def test_upload_page_http_4xx_returns_false(pdf_path: Path, dummy_image: Image.Image):
    """HTTP 4xx returns False without retry."""
    with patch.dict(os.environ, {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t"}):
        from uploader import _legacy_upload_page as upload_page

        err_resp = _make_response(401, {})
        with patch("uploader.requests.post", return_value=err_resp) as mock_post:
            result = upload_page(pdf_path, 1, 1, dummy_image)
    assert result is False
    assert mock_post.call_count == 1  # No retry for 4xx


def test_upload_page_retries_on_5xx(pdf_path: Path, dummy_image: Image.Image):
    """Transient 5xx triggers retry — 503 twice then 200 = 3 total calls."""
    with patch.dict(
        os.environ,
        {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t", "UPLOAD_MAX_RETRIES": "3"},
    ):
        from uploader import _legacy_upload_page as upload_page

        err_resp = _make_response(503, {})
        ok_resp = _make_response(
            200,
            {
                "batch_id": "x",
                "images": [{"original_file_name": "f"}],
                "rejected": [],
            },
        )
        with patch(
            "uploader.requests.post",
            side_effect=[err_resp, err_resp, ok_resp],
        ) as mock_post:
            with patch("uploader.time.sleep"):  # Skip actual waits in tests
                result = upload_page(pdf_path, 1, 1, dummy_image)
    assert result is True
    assert mock_post.call_count == 3


def test_upload_page_exhausts_retries_returns_false(
    pdf_path: Path, dummy_image: Image.Image
):
    """Returns False when all retry attempts are exhausted."""
    with patch.dict(
        os.environ,
        {"BACKEND_BASE_URL": "http://x", "API_TOKEN": "t", "UPLOAD_MAX_RETRIES": "2"},
    ):
        from uploader import _legacy_upload_page as upload_page

        err_resp = _make_response(503, {})
        with patch("uploader.requests.post", return_value=err_resp):
            with patch("uploader.time.sleep"):
                result = upload_page(pdf_path, 1, 1, dummy_image)
    assert result is False
