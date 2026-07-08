"""Unit tests for scanner/pdf_processor.py — T012."""
import os
from unittest.mock import MagicMock, patch


def _make_mock_page(rotation: int = 0, width: int = 100, height: int = 100):
    """Return a MagicMock resembling a fitz.Page."""
    page = MagicMock()
    page.rotation = rotation
    pixmap = MagicMock()
    # pil_tobytes returns raw bytes; we'll patch PIL.Image.frombytes to return
    # a real small image in tests that need it.
    pixmap.pil_tobytes.return_value = b"\xff\xd8\xff"  # fake JPEG bytes
    pixmap.width = width
    pixmap.height = height
    pixmap.samples = b"\x80" * (width * height * 3)
    pixmap.n = 3
    page.get_pixmap.return_value = pixmap
    return page


def _make_mock_doc(pages):
    """Return a MagicMock resembling a fitz.Document."""
    doc = MagicMock()
    doc.__len__.return_value = len(pages)
    doc.__iter__.return_value = iter(pages)
    doc.__enter__.return_value = doc
    doc.__exit__.return_value = False
    doc.load_page.side_effect = lambda i: pages[i]
    return doc


def test_process_pdf_page_count(tmp_path):
    """process_pdf returns one entry per page."""
    pages = [_make_mock_page(rotation=0) for _ in range(3)]
    mock_doc = _make_mock_doc(pages)

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                from pdf_processor import process_pdf
                result = process_pdf(tmp_path / "test.pdf")

    assert len(result) == 3


def test_process_pdf_rotation_applied_when_page_rotated(tmp_path):
    """Page with rotation=90 triggers correction; rotation_applied is non-zero."""
    pages = [_make_mock_page(rotation=90)]
    mock_doc = _make_mock_doc(pages)

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                from pdf_processor import process_pdf
                result = process_pdf(tmp_path / "test.pdf")

    page_num, pil_image, orientation_uncertain, rotation_applied = result[0]
    assert rotation_applied == 90


def test_process_pdf_no_rotation_when_upright(tmp_path):
    """Page with rotation=0 and OSD confident upright → rotation_applied=0."""
    pages = [_make_mock_page(rotation=0)]
    mock_doc = _make_mock_doc(pages)
    osd_result = {"rotate": 0, "orientation_conf": 5.0}

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                with patch("pdf_processor.pytesseract.image_to_osd", return_value=osd_result):
                    from pdf_processor import process_pdf
                    result = process_pdf(tmp_path / "test.pdf")

    page_num, pil_image, orientation_uncertain, rotation_applied = result[0]
    assert rotation_applied == 0


def test_process_pdf_pytesseract_fallback_called_when_pdf_rotation_zero(tmp_path):
    """When PDF metadata rotation=0, pytesseract OSD is called as tier-2."""
    pages = [_make_mock_page(rotation=0)]
    mock_doc = _make_mock_doc(pages)
    osd_result = {"rotate": 0, "orientation_conf": 5.0}

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                with patch(
                    "pdf_processor.pytesseract.image_to_osd",
                    return_value=osd_result,
                ) as mock_osd:
                    from pdf_processor import process_pdf

                    process_pdf(tmp_path / "test.pdf")

    mock_osd.assert_called_once()


def test_process_pdf_pytesseract_not_called_when_rotation_nonzero(tmp_path):
    """When PDF metadata rotation≠0, pytesseract OSD is NOT called (tier-1 sufficient)."""
    pages = [_make_mock_page(rotation=270)]
    mock_doc = _make_mock_doc(pages)

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                with patch("pdf_processor.pytesseract.image_to_osd") as mock_osd:
                    from pdf_processor import process_pdf
                    process_pdf(tmp_path / "test.pdf")

    mock_osd.assert_not_called()


def test_process_pdf_orientation_uncertain_when_both_tiers_fail(tmp_path):
    """orientation_uncertain=True when pytesseract raises and PDF rotation=0."""
    pages = [_make_mock_page(rotation=0)]
    mock_doc = _make_mock_doc(pages)

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                with patch(
                    "pdf_processor.pytesseract.image_to_osd",
                    side_effect=Exception("tesseract not found"),
                ):
                    from pdf_processor import process_pdf
                    result = process_pdf(tmp_path / "test.pdf")

    page_num, pil_image, orientation_uncertain, rotation_applied = result[0]
    assert orientation_uncertain is True
    assert rotation_applied == 0


def test_process_pdf_page_num_is_one_indexed(tmp_path):
    """process_pdf returns 1-indexed page numbers."""
    pages = [_make_mock_page() for _ in range(2)]
    mock_doc = _make_mock_doc(pages)

    with patch.dict(os.environ, {"BACKEND_BASE_URL": "https://x", "API_TOKEN": "t"}):
        with patch("pdf_processor.fitz.open", return_value=mock_doc):
            with patch("pdf_processor.Image.frombytes") as mock_frombytes:
                mock_frombytes.return_value = MagicMock()
                from pdf_processor import process_pdf
                result = process_pdf(tmp_path / "test.pdf")

    page_nums = [r[0] for r in result]
    assert page_nums == [1, 2]


# --- coverage: TIFF path (004) + OSD-failure branch ---

from pathlib import Path  # noqa: E402

from PIL import Image  # noqa: E402


def _make_tiff(path: Path, frames: int) -> None:
    imgs = [
        Image.new("RGB", (40, 40), color=(i * 30, i * 30, i * 30))
        for i in range(frames)
    ]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], format="TIFF")


def test_process_tiff_multi_frame_no_rotation(tmp_path: Path) -> None:
    p = tmp_path / "scan.tiff"
    _make_tiff(p, 3)
    with patch(
        "pdf_processor.pytesseract.image_to_osd",
        return_value={"rotate": 0, "orientation_conf": 9.0},
    ):
        from pdf_processor import process_pdf

        result = process_pdf(p)
    assert [r[0] for r in result] == [1, 2, 3]
    assert all(r[3] == 0 and r[2] is False for r in result)


def test_process_tiff_applies_osd_rotation(tmp_path: Path) -> None:
    p = tmp_path / "rot.tif"
    _make_tiff(p, 1)
    with patch(
        "pdf_processor.pytesseract.image_to_osd",
        return_value={"rotate": 90, "orientation_conf": 8.0},
    ):
        from pdf_processor import process_pdf

        result = process_pdf(p)
    page_num, image, uncertain, rotation = result[0]
    assert rotation == 90 and uncertain is False
    assert isinstance(image, Image.Image)


def test_process_tiff_osd_failure_marks_uncertain(tmp_path: Path) -> None:
    import pytesseract

    p = tmp_path / "bad.tiff"
    _make_tiff(p, 1)
    with patch(
        "pdf_processor.pytesseract.image_to_osd",
        side_effect=pytesseract.TesseractError(1, "no osd"),
    ):
        from pdf_processor import process_pdf

        result = process_pdf(p)
    _n, _img, uncertain, rotation = result[0]
    assert uncertain is True
    assert rotation == 0


# ---------------------------------------------------------------------------
# downsample_image — upload-size fallback for oversized pages
# ---------------------------------------------------------------------------


def test_downsample_image_halves_dimensions_at_150_dpi() -> None:
    from pdf_processor import RENDER_DPI, downsample_image

    assert RENDER_DPI == 300
    src = Image.new("RGB", (2550, 3300), color=(0, 0, 0))
    out = downsample_image(src, 150)
    assert out.size == (1275, 1650)


def test_downsample_image_quarters_dimensions_at_75_dpi() -> None:
    from pdf_processor import downsample_image

    src = Image.new("RGB", (2550, 3300), color=(0, 0, 0))
    out = downsample_image(src, 75)
    assert out.size == (638, 825)  # 2550*0.25 = 637.5 → round() → 638


def test_downsample_image_noop_at_or_above_base() -> None:
    from pdf_processor import downsample_image

    src = Image.new("RGB", (100, 100), color=(0, 0, 0))
    assert downsample_image(src, 300) is src
    assert downsample_image(src, 600) is src


def test_downsample_image_never_zero_sized() -> None:
    from pdf_processor import downsample_image

    src = Image.new("RGB", (2, 2), color=(0, 0, 0))
    out = downsample_image(src, 1)  # extreme reduction
    assert out.width >= 1 and out.height >= 1
