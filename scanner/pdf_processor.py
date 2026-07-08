"""
PDF and TIFF page extraction with orientation correction.

Two-tier orientation detection (PDF)
------------------------------------
Tier 1 — PDF metadata: `page.rotation` (fitz). Non-zero → apply correction.
Tier 2 — pytesseract OSD fallback: called only when tier-1 reports 0° rotation.
          On any tesseract failure the page is flagged orientation_uncertain=True
          and processed as-is (rotation_applied=0).

TIFF orientation detection
--------------------------
TIFFs have no embedded page-rotation metadata equivalent to PDF, so every frame
goes straight to the OSD path. EXIF Orientation (if present) is honored before
OSD via PIL's ImageOps.exif_transpose.
"""

import logging
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

_TIFF_EXTS = {".tif", ".tiff"}

# Base render resolution. Pages are rasterized at this DPI (see ``_render_page``)
# to preserve scan fidelity; ``downsample_image`` reduces a single page relative
# to this when the backend rejects it as too large.
RENDER_DPI = 300


def process_pdf(path: Path) -> list[tuple[int, Image.Image, bool, int]]:
    """
    Render every page/frame of *path* to a PIL Image, correcting orientation where possible.

    Despite the name, this dispatches to the TIFF path when *path* is a .tif/.tiff.

    Returns
    -------
    list of (page_num, pil_image, orientation_uncertain, rotation_applied)
        page_num            1-indexed
        pil_image           PIL Image after any rotation correction
        orientation_uncertain  True if neither tier could determine orientation
        rotation_applied    Degrees corrected (0, 90, 180, 270)
    """
    if path.suffix.lower() in _TIFF_EXTS:
        return _process_tiff(path)
    return _process_pdf(path)


# ---------------------------------------------------------------------------
# PDF path
# ---------------------------------------------------------------------------


def _process_pdf(path: Path) -> list[tuple[int, Image.Image, bool, int]]:
    results: list[tuple[int, Image.Image, bool, int]] = []

    with fitz.open(str(path)) as doc:
        total = len(doc)
        logger.debug("Opened %s — %d page(s)", path.name, total)

        for idx in range(total):
            page = doc.load_page(idx)
            page_num = idx + 1

            rotation_applied, orientation_uncertain = _detect_orientation(page, page_num, path)

            pil_image = _render_page(page)

            if rotation_applied:
                pil_image = pil_image.rotate(-rotation_applied, expand=True)

            results.append((page_num, pil_image, orientation_uncertain, rotation_applied))

    return results


def _detect_orientation(
    page: fitz.Page,
    page_num: int,
    path: Path,
) -> tuple[int, bool]:
    """
    Determine how many degrees to rotate the page to make it upright.

    Returns (rotation_applied, orientation_uncertain).
    """
    # Tier 1: PDF embedded rotation metadata
    pdf_rotation = page.rotation  # 0, 90, 180, or 270
    if pdf_rotation != 0:
        logger.debug(
            "%s p%d: tier-1 rotation=%d° from PDF metadata",
            path.name,
            page_num,
            pdf_rotation,
        )
        return pdf_rotation, False

    # Tier 2: pytesseract OSD on the rendered page
    pil_image = _render_page(page, dpi=RENDER_DPI)
    return _osd_rotation_from_image(pil_image, page_num, path)


def _render_page(page: fitz.Page, dpi: int = RENDER_DPI) -> Image.Image:
    """Render a fitz page to a PIL Image (RGB) at the given DPI.

    Defaults to 300 DPI so uploaded pages preserve scan-quality resolution
    (no downsampling). PDFs carry no intrinsic raster resolution, so 72 DPI
    would rasterize scanned pages far below their original fidelity.
    """
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(matrix=matrix)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


# ---------------------------------------------------------------------------
# TIFF path
# ---------------------------------------------------------------------------


def _process_tiff(path: Path) -> list[tuple[int, Image.Image, bool, int]]:
    results: list[tuple[int, Image.Image, bool, int]] = []

    with Image.open(path) as img:
        n_frames = getattr(img, "n_frames", 1)
        logger.debug("Opened %s — %d frame(s)", path.name, n_frames)

        for idx in range(n_frames):
            img.seek(idx)
            page_num = idx + 1

            # Honor any EXIF Orientation tag, then convert to RGB.
            pil_image = ImageOps.exif_transpose(img).convert("RGB")

            rotation_applied, orientation_uncertain = _osd_rotation_from_image(
                pil_image, page_num, path
            )

            if rotation_applied:
                pil_image = pil_image.rotate(-rotation_applied, expand=True)

            results.append((page_num, pil_image, orientation_uncertain, rotation_applied))

    return results


# ---------------------------------------------------------------------------
# Shared OSD helpers
# ---------------------------------------------------------------------------


def _osd_rotation_from_image(
    pil_image: Image.Image,
    page_num: int,
    path: Path,
) -> tuple[int, bool]:
    """
    Run pytesseract OSD against a PIL image. Returns (rotation_degrees, uncertain).
    """
    try:
        osd = pytesseract.image_to_osd(pil_image)
        if isinstance(osd, dict):
            rotate = int(osd.get("rotate", 0))
            conf = float(osd.get("orientation_conf", 0.0))
        else:
            rotate, conf = _parse_osd_string(str(osd))

        logger.debug(
            "%s p%d: OSD rotate=%d° conf=%.1f", path.name, page_num, rotate, conf
        )
        return rotate, False

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s p%d: OSD failed (%s) — orientation_uncertain=True",
            path.name,
            page_num,
            exc,
        )
        return 0, True


def downsample_image(
    image: Image.Image, to_dpi: int, *, base_dpi: int = RENDER_DPI
) -> Image.Image:
    """Return *image* scaled from ``base_dpi`` down to ``to_dpi``.

    Used as an upload-size fallback: a page the backend rejects as too large is
    re-encoded at a lower effective resolution. Only the single offending page
    is shrunk — the rest of the document keeps full ``RENDER_DPI`` fidelity.
    LANCZOS resampling keeps scanned text legible at the reduced size. A
    ``to_dpi`` at or above ``base_dpi`` is a no-op (returns the original).
    """
    if to_dpi >= base_dpi:
        return image
    factor = to_dpi / base_dpi
    new_size = (
        max(1, round(image.width * factor)),
        max(1, round(image.height * factor)),
    )
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _parse_osd_string(osd_text: str) -> tuple[int, float]:
    """Parse key-value pairs from tesseract OSD text output."""
    rotate = 0
    conf = 0.0
    for line in osd_text.splitlines():
        if "Rotate:" in line:
            try:
                rotate = int(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
        elif "Orientation confidence:" in line:
            try:
                conf = float(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
    return rotate, conf
